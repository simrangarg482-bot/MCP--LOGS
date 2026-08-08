"""Live integration test for `app.ingestion.connectors.confluence.ConfluenceConnector`.

WHAT THIS VERIFIES (real network calls, no mocking -- separate from the
existing, untouched, fully-mocked `tests/ingestion/connectors/test_confluence.py`)
    1. `authenticate()` succeeds against the real
       `GET {base_url}/wiki/rest/api/space?limit=1`.
    2. `authenticate()` correctly REJECTS invalid Basic-auth credentials.
    3. `fetch_batch()` returns real content via a real CQL search and every
       item normalizes into a well-formed `RawDocument` (metadata always
       includes `space` and `kind`).
    4. Pagination: Confluence's CQL search has no reliable total count, so
       this connector uses a page-length heuristic (`len(raw results) <
       page size of 25`) -- if a full page came back, fetches the next page
       via `cursor=next_cursor` and confirms it succeeds.
    5. `since` (a real CQL `lastmodified >= "..."` clause, server-side):
       confirms `since=now()` returns no more content than an unfiltered
       search.

REQUIRES (tests/ingestion_retrieval/.env)
    EKIP_TEST_CONFLUENCE_BASE_URL, EKIP_TEST_CONFLUENCE_EMAIL,
    EKIP_TEST_CONFLUENCE_API_TOKEN, EKIP_TEST_CONFLUENCE_SPACES
    (comma-separated space keys)

RUN
    pytest scripts/live_connector_tests/test_confluence_live.py -v -s
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.ingestion.connectors.confluence import ConfluenceConnector
from app.ingestion.schemas import RawDocument, ResolvedConnectorConfig


def _build_config(credential_ref: str, config: dict, organization_id: uuid.UUID) -> ResolvedConnectorConfig:
    return ResolvedConnectorConfig(
        connector_config_id=uuid.uuid4(),
        organization_id=organization_id,
        project_id=None,
        source="confluence",
        credential_ref=credential_ref,
        config=config,
    )


@pytest.mark.asyncio
async def test_authenticate_succeeds_with_real_credentials(confluence_spec, organization_id):
    connector = ConfluenceConnector()
    resolved = _build_config(confluence_spec.credential_ref, confluence_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    assert client is not None
    await connector.close(client)
    print("PASS: Confluence authenticate() succeeded against real GET {base_url}/wiki/rest/api/space?limit=1")


@pytest.mark.asyncio
async def test_authenticate_rejects_invalid_credentials(confluence_spec, organization_id):
    connector = ConfluenceConnector()
    resolved = _build_config(
        "not-a-real-email@example.com:not-a-real-token", confluence_spec.config, organization_id
    )
    with pytest.raises(Exception):
        await connector.authenticate(resolved)
    print("PASS: Confluence authenticate() correctly rejected invalid credentials")


@pytest.mark.asyncio
async def test_fetch_and_normalize_real_content(confluence_spec, organization_id):
    connector = ConfluenceConnector()
    resolved = _build_config(confluence_spec.credential_ref, confluence_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        result = await connector.fetch_batch(client, since=None, cursor=None)
        assert isinstance(result.items, list)
        print(f"Fetched {len(result.items)} raw item(s) from Confluence. has_more={result.has_more}")

        if not result.items:
            pytest.skip(
                "Configured space(s) returned zero pages/blogposts/comments/attachments -- nothing "
                "to normalize. Not a connector failure; point EKIP_TEST_CONFLUENCE_SPACES at a "
                "space with real content."
            )

        for raw_item in result.items:
            doc = connector.normalize(raw_item)
            assert isinstance(doc, RawDocument)
            assert doc.source == "confluence"
            assert doc.external_id
            assert "space" in doc.metadata
            assert "kind" in doc.metadata
        print(f"PASS: normalized {len(result.items)} real Confluence item(s) into well-formed RawDocuments")
    finally:
        await connector.close(client)


@pytest.mark.asyncio
async def test_pagination_next_page_is_fetchable(confluence_spec, organization_id):
    connector = ConfluenceConnector()
    resolved = _build_config(confluence_spec.credential_ref, confluence_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        first_page = await connector.fetch_batch(client, since=None, cursor=None)
        if not first_page.has_more:
            pytest.skip(
                "Configured space(s) fit in a single page (< 25 raw results) -- pagination path "
                "not exercised by this data. Not a failure."
            )
        second_page = await connector.fetch_batch(client, since=None, cursor=first_page.next_cursor)
        assert isinstance(second_page.items, list)
        print(f"PASS: fetched a real second page via next_cursor ({len(second_page.items)} item(s))")
    finally:
        await connector.close(client)


@pytest.mark.asyncio
async def test_since_filter_reduces_or_equals_results(confluence_spec, organization_id):
    connector = ConfluenceConnector()
    resolved = _build_config(confluence_spec.credential_ref, confluence_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        unfiltered = await connector.fetch_batch(client, since=None, cursor=None)
        very_recent = await connector.fetch_batch(client, since=datetime.now(timezone.utc), cursor=None)
        assert len(very_recent.items) <= len(unfiltered.items)
        print(
            f"PASS: since=now() (real CQL `lastmodified >= ...` clause) returned "
            f"{len(very_recent.items)} item(s) vs {len(unfiltered.items)} unfiltered"
        )
    finally:
        await connector.close(client)
