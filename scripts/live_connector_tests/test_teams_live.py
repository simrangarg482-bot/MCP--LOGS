"""Live integration test for `app.ingestion.connectors.teams.TeamsConnector`.

WHAT THIS VERIFIES (real network calls, no mocking -- separate from the
existing, untouched, fully-mocked `tests/ingestion/connectors/test_teams.py`)
    1. `authenticate()` succeeds against the real
       `GET https://graph.microsoft.com/v1.0/me`.
    2. `authenticate()` correctly REJECTS an invalid bearer token.
    3. `fetch_batch()` with `since=None` runs a real FULL SYNC (plain
       `GET .../messages`, no delta) and every item normalizes into a
       well-formed `RawDocument` (metadata always includes `channel_id`).
    4. Pagination: follows Microsoft Graph's real `@odata.nextLink` (passed
       through verbatim as this connector's opaque `next_cursor`, per
       `teams.py`) for a second page, if one exists.
    5. `since` set runs the OTHER code path entirely -- a Graph delta query
       with `$filter=lastModifiedDateTime gt ...` -- not just a filtered
       version of the full-sync call. This test only asserts the delta call
       itself succeeds and returns a list (a strict "fewer items" comparison
       against full sync isn't apples-to-apples across two different Graph
       endpoints, so this test does not assert item-count ordering, only
       that both paths work).

REQUIRES (tests/ingestion_retrieval/.env)
    EKIP_TEST_TEAMS_ACCESS_TOKEN (an ALREADY-ISSUED Graph bearer token --
    typically expires in ~1 hour; re-issue and re-paste if this test starts
    failing with 401), EKIP_TEST_TEAMS_TEAM_ID, EKIP_TEST_TEAMS_CHANNEL_IDS

RUN
    pytest scripts/live_connector_tests/test_teams_live.py -v -s
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.ingestion.connectors.teams import TeamsConnector
from app.ingestion.schemas import RawDocument, ResolvedConnectorConfig


def _build_config(credential_ref: str, config: dict, organization_id: uuid.UUID) -> ResolvedConnectorConfig:
    return ResolvedConnectorConfig(
        connector_config_id=uuid.uuid4(),
        organization_id=organization_id,
        project_id=None,
        source="teams",
        credential_ref=credential_ref,
        config=config,
    )


@pytest.mark.asyncio
async def test_authenticate_succeeds_with_real_token(teams_spec, organization_id):
    connector = TeamsConnector()
    resolved = _build_config(teams_spec.credential_ref, teams_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    assert client is not None
    await connector.close(client)
    print("PASS: Teams authenticate() succeeded against real GET https://graph.microsoft.com/v1.0/me")


@pytest.mark.asyncio
async def test_authenticate_rejects_invalid_token(teams_spec, organization_id):
    connector = TeamsConnector()
    resolved = _build_config("Bearer-token-that-is-not-real", teams_spec.config, organization_id)
    with pytest.raises(Exception):
        await connector.authenticate(resolved)
    print("PASS: Teams authenticate() correctly rejected an invalid token")


@pytest.mark.asyncio
async def test_fetch_and_normalize_real_messages_full_sync(teams_spec, organization_id):
    connector = TeamsConnector()
    resolved = _build_config(teams_spec.credential_ref, teams_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        result = await connector.fetch_batch(client, since=None, cursor=None)
        assert isinstance(result.items, list)
        print(f"Fetched {len(result.items)} raw message(s) from Teams (full sync). has_more={result.has_more}")

        if not result.items:
            pytest.skip(
                "Configured channel(s) returned zero messages -- nothing to normalize. Not a "
                "connector failure; point EKIP_TEST_TEAMS_CHANNEL_IDS at a channel with real history."
            )

        for raw_item in result.items:
            doc = connector.normalize(raw_item)
            assert isinstance(doc, RawDocument)
            assert doc.source == "teams"
            assert doc.external_id
            assert "channel_id" in doc.metadata
        print(f"PASS: normalized {len(result.items)} real Teams message(s) into well-formed RawDocuments")
    finally:
        await connector.close(client)


@pytest.mark.asyncio
async def test_pagination_second_page_is_fetchable(teams_spec, organization_id):
    connector = TeamsConnector()
    resolved = _build_config(teams_spec.credential_ref, teams_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        first_page = await connector.fetch_batch(client, since=None, cursor=None)
        if not first_page.has_more:
            pytest.skip(
                "Configured channel(s) fit in a single page (Graph returned no @odata.nextLink) -- "
                "pagination path not exercised by this data. Not a failure."
            )
        second_page = await connector.fetch_batch(client, since=None, cursor=first_page.next_cursor)
        assert isinstance(second_page.items, list)
        print(f"PASS: fetched a real second page via Graph's @odata.nextLink ({len(second_page.items)} item(s))")
    finally:
        await connector.close(client)


@pytest.mark.asyncio
async def test_incremental_delta_query_succeeds(teams_spec, organization_id):
    connector = TeamsConnector()
    resolved = _build_config(teams_spec.credential_ref, teams_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        delta_result = await connector.fetch_batch(client, since=datetime.now(timezone.utc), cursor=None)
        assert isinstance(delta_result.items, list)
        print(
            f"PASS: real Graph delta query (`$filter=lastModifiedDateTime gt ...`) succeeded, "
            f"returned {len(delta_result.items)} item(s) for since=now()"
        )
    finally:
        await connector.close(client)
