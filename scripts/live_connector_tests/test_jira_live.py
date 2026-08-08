"""Live integration test for `app.ingestion.connectors.jira.JiraConnector`.

WHAT THIS VERIFIES (real network calls, no mocking -- separate from the
existing, untouched, fully-mocked `tests/ingestion/connectors/test_jira.py`)
    1. `authenticate()` succeeds against the real
       `GET {base_url}/rest/api/2/myself`.
    2. `authenticate()` correctly REJECTS invalid Basic-auth credentials.
    3. `fetch_batch()` returns real issues via a real JQL search and every
       item normalizes into a well-formed `RawDocument` (metadata always
       includes `project`).
    4. Pagination: Jira reports exhaustion via its own `total` count (NOT a
       "fewer than page size" heuristic, per `jira.py`) -- if
       `start_at + len(issues) < total`, fetches the next page via
       `cursor=next_cursor` and confirms it succeeds.
    5. `since` (a real JQL `updated >= "..."` clause, server-side): confirms
       `since=now()` returns no more issues than an unfiltered search.

REQUIRES (tests/ingestion_retrieval/.env)
    EKIP_TEST_JIRA_BASE_URL, EKIP_TEST_JIRA_EMAIL, EKIP_TEST_JIRA_API_TOKEN,
    EKIP_TEST_JIRA_PROJECTS (comma-separated project keys)

RUN
    pytest scripts/live_connector_tests/test_jira_live.py -v -s
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.ingestion.connectors.jira import JiraConnector
from app.ingestion.schemas import RawDocument, ResolvedConnectorConfig


def _build_config(credential_ref: str, config: dict, organization_id: uuid.UUID) -> ResolvedConnectorConfig:
    return ResolvedConnectorConfig(
        connector_config_id=uuid.uuid4(),
        organization_id=organization_id,
        project_id=None,
        source="jira",
        credential_ref=credential_ref,
        config=config,
    )


@pytest.mark.asyncio
async def test_authenticate_succeeds_with_real_credentials(jira_spec, organization_id):
    connector = JiraConnector()
    resolved = _build_config(jira_spec.credential_ref, jira_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    assert client is not None
    await connector.close(client)
    print("PASS: Jira authenticate() succeeded against real GET {base_url}/rest/api/2/myself")


@pytest.mark.asyncio
async def test_authenticate_rejects_invalid_credentials(jira_spec, organization_id):
    connector = JiraConnector()
    resolved = _build_config("not-a-real-email@example.com:not-a-real-token", jira_spec.config, organization_id)
    with pytest.raises(Exception):
        await connector.authenticate(resolved)
    print("PASS: Jira authenticate() correctly rejected invalid credentials")


@pytest.mark.asyncio
async def test_fetch_and_normalize_real_issues(jira_spec, organization_id):
    connector = JiraConnector()
    resolved = _build_config(jira_spec.credential_ref, jira_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        result = await connector.fetch_batch(client, since=None, cursor=None)
        assert isinstance(result.items, list)
        print(f"Fetched {len(result.items)} raw issue(s) from Jira. has_more={result.has_more}")

        if not result.items:
            pytest.skip(
                "Configured project(s) returned zero issues -- nothing to normalize. Not a "
                "connector failure; point EKIP_TEST_JIRA_PROJECTS at a project with real issues."
            )

        for raw_item in result.items:
            doc = connector.normalize(raw_item)
            assert isinstance(doc, RawDocument)
            assert doc.source == "jira"
            assert doc.external_id
            assert "project" in doc.metadata
        print(f"PASS: normalized {len(result.items)} real Jira issue(s) into well-formed RawDocuments")
    finally:
        await connector.close(client)


@pytest.mark.asyncio
async def test_pagination_next_page_is_fetchable(jira_spec, organization_id):
    connector = JiraConnector()
    resolved = _build_config(jira_spec.credential_ref, jira_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        first_page = await connector.fetch_batch(client, since=None, cursor=None)
        if not first_page.has_more:
            pytest.skip(
                "Configured project(s) fit in a single page (Jira's own `total` count showed no "
                "more results) -- pagination path not exercised by this data. Not a failure."
            )
        second_page = await connector.fetch_batch(client, since=None, cursor=first_page.next_cursor)
        assert isinstance(second_page.items, list)
        print(f"PASS: fetched a real second page via next_cursor ({len(second_page.items)} item(s))")
    finally:
        await connector.close(client)


@pytest.mark.asyncio
async def test_since_filter_reduces_or_equals_results(jira_spec, organization_id):
    connector = JiraConnector()
    resolved = _build_config(jira_spec.credential_ref, jira_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        unfiltered = await connector.fetch_batch(client, since=None, cursor=None)
        very_recent = await connector.fetch_batch(client, since=datetime.now(timezone.utc), cursor=None)
        assert len(very_recent.items) <= len(unfiltered.items)
        print(
            f"PASS: since=now() (real JQL `updated >= ...` clause) returned {len(very_recent.items)} "
            f"issue(s) vs {len(unfiltered.items)} unfiltered"
        )
    finally:
        await connector.close(client)
