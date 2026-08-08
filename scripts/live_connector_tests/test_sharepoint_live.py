"""Live integration test for `app.ingestion.connectors.sharepoint.SharePointConnector`.

WHAT THIS VERIFIES (real network calls, no mocking -- separate from the
existing, untouched, fully-mocked `tests/ingestion/connectors/test_sharepoint.py`)
    1. `authenticate()` succeeds against the real
       `GET https://graph.microsoft.com/v1.0/me`.
    2. `authenticate()` correctly REJECTS an invalid bearer token.
    3. `fetch_batch()` returns real files (pre-filtered by this connector to
       supported extensions: .txt/.md/.markdown/.pdf/.docx/.xlsx) via
       Microsoft Graph's delta endpoint, and every item normalizes into a
       well-formed `RawDocument` (metadata always includes `site_id`).
    4. Pagination: follows Graph's real `@odata.nextLink` for a second page,
       if one exists. NOTE: unlike most other connectors, SharePoint has no
       page-size constant at all -- `has_more` is driven purely by
       `@odata.nextLink` presence, not a length comparison (see this
       connector's own module docstring).
    5. `since`: this connector's docstring states Graph's delta response
       carries no `$filter`-style query support, so filtering is CLIENT-SIDE
       (`_is_recent_enough`, applied per-entry inside `fetch_batch` before
       download) -- confirms `since=now()` still returns no more items than
       an unfiltered fetch, even though the filtering happens after the
       Graph call rather than as a query parameter.

REQUIRES (tests/ingestion_retrieval/.env)
    EKIP_TEST_SHAREPOINT_ACCESS_TOKEN (an ALREADY-ISSUED Graph bearer token
    -- typically expires in ~1 hour; re-issue and re-paste if this test
    starts failing with 401), EKIP_TEST_SHAREPOINT_SITE_IDS

RUN
    pytest scripts/live_connector_tests/test_sharepoint_live.py -v -s
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.ingestion.connectors.sharepoint import SharePointConnector
from app.ingestion.schemas import RawDocument, ResolvedConnectorConfig


def _build_config(credential_ref: str, config: dict, organization_id: uuid.UUID) -> ResolvedConnectorConfig:
    return ResolvedConnectorConfig(
        connector_config_id=uuid.uuid4(),
        organization_id=organization_id,
        project_id=None,
        source="sharepoint",
        credential_ref=credential_ref,
        config=config,
    )


@pytest.mark.asyncio
async def test_authenticate_succeeds_with_real_token(sharepoint_spec, organization_id):
    connector = SharePointConnector()
    resolved = _build_config(sharepoint_spec.credential_ref, sharepoint_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    assert client is not None
    await connector.close(client)
    print("PASS: SharePoint authenticate() succeeded against real GET https://graph.microsoft.com/v1.0/me")


@pytest.mark.asyncio
async def test_authenticate_rejects_invalid_token(sharepoint_spec, organization_id):
    connector = SharePointConnector()
    resolved = _build_config("Bearer-token-that-is-not-real", sharepoint_spec.config, organization_id)
    with pytest.raises(Exception):
        await connector.authenticate(resolved)
    print("PASS: SharePoint authenticate() correctly rejected an invalid token")


@pytest.mark.asyncio
async def test_fetch_and_normalize_real_files(sharepoint_spec, organization_id):
    connector = SharePointConnector()
    resolved = _build_config(sharepoint_spec.credential_ref, sharepoint_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        result = await connector.fetch_batch(client, since=None, cursor=None)
        assert isinstance(result.items, list)
        print(f"Fetched {len(result.items)} raw file item(s) from SharePoint. has_more={result.has_more}")

        if not result.items:
            pytest.skip(
                "Configured site(s) returned zero supported files (.txt/.md/.markdown/.pdf/"
                ".docx/.xlsx) -- nothing to normalize. Not a connector failure; point "
                "EKIP_TEST_SHAREPOINT_SITE_IDS at a site with real supported documents."
            )

        for raw_item in result.items:
            doc = connector.normalize(raw_item)
            assert isinstance(doc, RawDocument)
            assert doc.source == "sharepoint"
            assert doc.external_id
            assert "site_id" in doc.metadata
        print(f"PASS: normalized {len(result.items)} real SharePoint file(s) into well-formed RawDocuments")
    finally:
        await connector.close(client)


@pytest.mark.asyncio
async def test_pagination_second_page_is_fetchable(sharepoint_spec, organization_id):
    connector = SharePointConnector()
    resolved = _build_config(sharepoint_spec.credential_ref, sharepoint_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        first_page = await connector.fetch_batch(client, since=None, cursor=None)
        if not first_page.has_more:
            pytest.skip(
                "Configured site(s) fit in a single page (Graph returned no @odata.nextLink) -- "
                "pagination path not exercised by this data. Not a failure."
            )
        second_page = await connector.fetch_batch(client, since=None, cursor=first_page.next_cursor)
        assert isinstance(second_page.items, list)
        print(f"PASS: fetched a real second page via Graph's @odata.nextLink ({len(second_page.items)} item(s))")
    finally:
        await connector.close(client)


@pytest.mark.asyncio
async def test_since_filter_reduces_or_equals_results(sharepoint_spec, organization_id):
    connector = SharePointConnector()
    resolved = _build_config(sharepoint_spec.credential_ref, sharepoint_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        unfiltered = await connector.fetch_batch(client, since=None, cursor=None)
        very_recent = await connector.fetch_batch(client, since=datetime.now(timezone.utc), cursor=None)
        assert len(very_recent.items) <= len(unfiltered.items)
        print(
            f"PASS: since=now() (client-side `_is_recent_enough` filter) returned "
            f"{len(very_recent.items)} item(s) vs {len(unfiltered.items)} unfiltered"
        )
    finally:
        await connector.close(client)
