"""Live integration test for `app.ingestion.connectors.slack.SlackConnector`.

WHAT THIS VERIFIES (real network calls, no mocking -- see this directory's
README.md for why this is separate from the existing, untouched
`tests/ingestion/connectors/test_slack.py`, which is fully mocked)
    1. `authenticate()` succeeds against the real
       `POST https://slack.com/api/auth.test`.
    2. `authenticate()` correctly REJECTS an invalid bot token (a cheap,
       safe negative test -- makes the same real call with garbage
       credentials, asserts it raises).
    3. `fetch_batch()` returns real messages from the configured channel(s)
       and every item `normalize()`s into a well-formed `RawDocument`
       (source == "slack", non-empty external_id/content, `channel_id` in
       metadata).
    4. Pagination: if the channel has more than one page of history (Slack's
       own `has_more`/`next_cursor` response fields -- not a length
       heuristic, per `slack.py`), fetches the second page via
       `cursor=first_page.next_cursor` and confirms it succeeds. SKIPPED
       (not failed) if the channel fits in one page.
    5. `since` (Slack's real `oldest` query param, server-side): confirms
       `since=now()` returns no more items than an unfiltered fetch.

REQUIRES (tests/ingestion_retrieval/.env)
    EKIP_TEST_SLACK_BOT_TOKEN, EKIP_TEST_SLACK_CHANNEL_IDS
    Bot token scopes needed: channels:history, channels:read (or the
    group: equivalents for a private channel).

RUN
    pytest scripts/live_connector_tests/test_slack_live.py -v -s
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.ingestion.connectors.slack import SlackConnector
from app.ingestion.schemas import RawDocument, ResolvedConnectorConfig


def _build_config(credential_ref: str, config: dict, organization_id: uuid.UUID) -> ResolvedConnectorConfig:
    return ResolvedConnectorConfig(
        connector_config_id=uuid.uuid4(),
        organization_id=organization_id,
        project_id=None,
        source="slack",
        credential_ref=credential_ref,
        config=config,
    )


@pytest.mark.asyncio
async def test_authenticate_succeeds_with_real_token(slack_spec, organization_id):
    connector = SlackConnector()
    resolved = _build_config(slack_spec.credential_ref, slack_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    assert client is not None
    await connector.close(client)
    print("PASS: Slack authenticate() succeeded against real POST https://slack.com/api/auth.test")


@pytest.mark.asyncio
async def test_authenticate_rejects_invalid_token(slack_spec, organization_id):
    connector = SlackConnector()
    resolved = _build_config("xoxb-deliberately-invalid-token", slack_spec.config, organization_id)
    with pytest.raises(Exception):
        await connector.authenticate(resolved)
    print("PASS: Slack authenticate() correctly rejected an invalid bot token")


@pytest.mark.asyncio
async def test_fetch_and_normalize_real_messages(slack_spec, organization_id):
    connector = SlackConnector()
    resolved = _build_config(slack_spec.credential_ref, slack_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        result = await connector.fetch_batch(client, since=None, cursor=None)
        assert isinstance(result.items, list)
        print(f"Fetched {len(result.items)} raw item(s) from Slack. has_more={result.has_more}")

        if not result.items:
            pytest.skip(
                "Configured channel(s) returned zero messages -- nothing to normalize. Not a "
                "connector failure; point EKIP_TEST_SLACK_CHANNEL_IDS at a channel with real history."
            )

        for raw_item in result.items:
            doc = connector.normalize(raw_item)
            assert isinstance(doc, RawDocument)
            assert doc.source == "slack"
            assert doc.external_id
            assert doc.content
            assert "channel_id" in doc.metadata
        print(f"PASS: normalized {len(result.items)} real Slack message(s) into well-formed RawDocuments")
    finally:
        await connector.close(client)


@pytest.mark.asyncio
async def test_pagination_second_page_is_fetchable(slack_spec, organization_id):
    connector = SlackConnector()
    resolved = _build_config(slack_spec.credential_ref, slack_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        first_page = await connector.fetch_batch(client, since=None, cursor=None)
        if not first_page.has_more:
            pytest.skip(
                "Configured channel(s) fit in a single page (Slack's own has_more/next_cursor "
                "flags were False) -- pagination path not exercised by this data. Not a failure."
            )
        second_page = await connector.fetch_batch(client, since=None, cursor=first_page.next_cursor)
        assert isinstance(second_page.items, list)
        print(f"PASS: fetched a real second page via next_cursor ({len(second_page.items)} item(s))")
    finally:
        await connector.close(client)


@pytest.mark.asyncio
async def test_since_filter_reduces_or_equals_results(slack_spec, organization_id):
    connector = SlackConnector()
    resolved = _build_config(slack_spec.credential_ref, slack_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        unfiltered = await connector.fetch_batch(client, since=None, cursor=None)
        very_recent = await connector.fetch_batch(client, since=datetime.now(timezone.utc), cursor=None)
        assert len(very_recent.items) <= len(unfiltered.items)
        print(
            f"PASS: since=now() (real Slack `oldest` param) returned {len(very_recent.items)} "
            f"item(s) vs {len(unfiltered.items)} unfiltered"
        )
    finally:
        await connector.close(client)
