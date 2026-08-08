"""Live integration test for `app.ingestion.connectors.azure_devops.AzureDevOpsConnector`.

WHAT THIS VERIFIES (real network calls, no mocking -- separate from the
existing, untouched, fully-mocked `tests/ingestion/connectors/test_azure_devops.py`)
    1. `authenticate()` succeeds against the real
       `GET https://dev.azure.com/{organization}/_apis/projects?api-version=7.1`.
    2. `authenticate()` correctly REJECTS an invalid PAT.
       NOTE (a real, disclosed limitation of the connector's own code, not
       of this test): Azure DevOps sometimes returns HTTP 203 with an HTML
       sign-in redirect for a bad/expired PAT instead of 401/403.
       `raise_for_status()` only raises on status >= 400, so a 203 response
       would NOT be caught as a failure by `authenticate()` as currently
       written. This test only asserts the case that IS handled (an
       outright rejected/malformed credential); it does not attempt to
       reproduce the specific 203 edge case, since that depends on
       Azure DevOps' own auth-redirect behavior, not something this test can
       reliably force.
    3. `fetch_batch()` returns real work items via a real WIQL query and
       every item normalizes into a well-formed `RawDocument` (metadata
       always includes `project`).
    4. Pagination: this connector re-runs the WIQL query on every call and
       compares against the total ID list length (NOT a page-size
       heuristic) -- if `next_batch_start < total_ids`, fetches the next
       batch via `cursor=next_cursor` and confirms it succeeds.
    5. `since` (a real WIQL `[System.ChangedDate] >= '...'` clause,
       server-side): confirms `since=now()` returns no more work items than
       an unfiltered query.

REQUIRES (tests/ingestion_retrieval/.env)
    EKIP_TEST_AZURE_DEVOPS_ORG, EKIP_TEST_AZURE_DEVOPS_PAT,
    EKIP_TEST_AZURE_DEVOPS_PROJECTS (comma-separated project names)

RUN
    pytest scripts/live_connector_tests/test_azure_devops_live.py -v -s
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.ingestion.connectors.azure_devops import AzureDevOpsConnector
from app.ingestion.schemas import RawDocument, ResolvedConnectorConfig


def _build_config(credential_ref: str, config: dict, organization_id: uuid.UUID) -> ResolvedConnectorConfig:
    return ResolvedConnectorConfig(
        connector_config_id=uuid.uuid4(),
        organization_id=organization_id,
        project_id=None,
        source="azure_devops",
        credential_ref=credential_ref,
        config=config,
    )


@pytest.mark.asyncio
async def test_authenticate_succeeds_with_real_pat(azure_devops_spec, organization_id):
    connector = AzureDevOpsConnector()
    resolved = _build_config(azure_devops_spec.credential_ref, azure_devops_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    assert client is not None
    await connector.close(client)
    print(
        "PASS: Azure DevOps authenticate() succeeded against real "
        "GET https://dev.azure.com/{organization}/_apis/projects?api-version=7.1"
    )


@pytest.mark.asyncio
async def test_authenticate_rejects_invalid_pat(azure_devops_spec, organization_id):
    connector = AzureDevOpsConnector()
    resolved = _build_config("deliberately-invalid-pat", azure_devops_spec.config, organization_id)
    with pytest.raises(Exception):
        await connector.authenticate(resolved)
    print("PASS: Azure DevOps authenticate() correctly rejected an invalid PAT")


@pytest.mark.asyncio
async def test_fetch_and_normalize_real_work_items(azure_devops_spec, organization_id):
    connector = AzureDevOpsConnector()
    resolved = _build_config(azure_devops_spec.credential_ref, azure_devops_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        result = await connector.fetch_batch(client, since=None, cursor=None)
        assert isinstance(result.items, list)
        print(f"Fetched {len(result.items)} raw work item(s) from Azure DevOps. has_more={result.has_more}")

        if not result.items:
            pytest.skip(
                "Configured project(s) returned zero work items -- nothing to normalize. Not a "
                "connector failure; point EKIP_TEST_AZURE_DEVOPS_PROJECTS at a project with real "
                "work items."
            )

        for raw_item in result.items:
            doc = connector.normalize(raw_item)
            assert isinstance(doc, RawDocument)
            assert doc.source == "azure_devops"
            assert doc.external_id
            assert "project" in doc.metadata
        print(f"PASS: normalized {len(result.items)} real Azure DevOps work item(s) into well-formed RawDocuments")
    finally:
        await connector.close(client)


@pytest.mark.asyncio
async def test_pagination_next_batch_is_fetchable(azure_devops_spec, organization_id):
    connector = AzureDevOpsConnector()
    resolved = _build_config(azure_devops_spec.credential_ref, azure_devops_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        first_page = await connector.fetch_batch(client, since=None, cursor=None)
        if not first_page.has_more:
            pytest.skip(
                "Configured project(s) fit in a single batch (WIQL result list exhausted) -- "
                "pagination path not exercised by this data. Not a failure."
            )
        second_page = await connector.fetch_batch(client, since=None, cursor=first_page.next_cursor)
        assert isinstance(second_page.items, list)
        print(f"PASS: fetched a real second batch via next_cursor ({len(second_page.items)} item(s))")
    finally:
        await connector.close(client)


@pytest.mark.asyncio
async def test_since_filter_reduces_or_equals_results(azure_devops_spec, organization_id):
    connector = AzureDevOpsConnector()
    resolved = _build_config(azure_devops_spec.credential_ref, azure_devops_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        unfiltered = await connector.fetch_batch(client, since=None, cursor=None)
        very_recent = await connector.fetch_batch(client, since=datetime.now(timezone.utc), cursor=None)
        assert len(very_recent.items) <= len(unfiltered.items)
        print(
            f"PASS: since=now() (real WIQL `[System.ChangedDate] >= ...` clause) returned "
            f"{len(very_recent.items)} work item(s) vs {len(unfiltered.items)} unfiltered"
        )
    finally:
        await connector.close(client)
