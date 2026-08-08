"""Live integration test for `app.ingestion.connectors.runbooks.RunbooksConnector`.

HOW THIS CONNECTOR DIFFERS FROM ALL 7 OTHERS (confirmed by reading
`runbooks.py` directly, not assumed)
    This is EKIP's one internal connector: `authenticate()` makes NO
    outbound network call at all -- it just constructs an `Identity` and
    bundles `organization_id`. `credential_ref` is unused (there is no
    external credential to hold). Its real "live" dependency is a real
    Postgres connection (`DATABASE_URL`, from the PROJECT's own root
    `.env`) and a real organization with actual approved/published
    postmortems in it -- not an external API or a token.
    Consequently: there is no "authenticate() rejects invalid credentials"
    test here (unlike every other connector in this suite) -- there is
    nothing to reject; any credential_ref value is accepted and ignored.

WHAT THIS VERIFIES (real database access, no mocking -- separate from the
existing, untouched, fully-mocked `tests/ingestion/connectors/test_runbooks.py`)
    1. `authenticate()` succeeds (constructs a client with no network call).
    2. `fetch_batch()` runs a real query
       (`core.incidents.service.list_postmortems_for_ingestion`) against the
       bootstrapped test organization's real postmortem data, and every item
       normalizes into a well-formed `RawDocument` (content synthesized from
       root_cause + action_items; metadata always includes `incident_id`,
       `status`, `generated_by`).
    3. Pagination: this connector uses a plain page-length heuristic
       (`has_more = len(items) == 50`) -- if a full page came back, fetches
       the next page via `cursor=next_cursor` and confirms it succeeds.
    4. `since` is passed straight through to
       `list_postmortems_for_ingestion` -- this test confirms the call
       succeeds and returns no more items than an unfiltered fetch, but
       does NOT independently verify that inner function's own SQL filter
       semantics (out of scope of `connectors/`, not re-verified here;
       flagged rather than assumed correct).

REQUIRES
    A real `DATABASE_URL` in the PROJECT's own root `.env` (same requirement
    as every other script in this project that touches the database). No
    entry needed in `tests/ingestion_retrieval/.env` for this connector.

    If the bootstrapped test organization (`EKIP_TEST_ORG_SLUG` in
    `tests/ingestion_retrieval/.env`, default "rag-pipeline-test") has no
    approved/published postmortems yet, the data-dependent tests below
    SKIP with a clear message rather than failing -- that is an honest,
    expected result for a fresh test organization, not a connector bug.

RUN
    pytest scripts/live_connector_tests/test_runbooks_live.py -v -s
"""

from __future__ import annotations

import uuid

import pytest

from app.ingestion.connectors.runbooks import RunbooksConnector
from app.ingestion.schemas import RawDocument, ResolvedConnectorConfig


def _build_config(organization_id: uuid.UUID) -> ResolvedConnectorConfig:
    return ResolvedConnectorConfig(
        connector_config_id=uuid.uuid4(),
        organization_id=organization_id,
        project_id=None,
        source="runbooks",
        credential_ref="unused",
        config={},
    )


@pytest.mark.asyncio
async def test_authenticate_succeeds_no_network_call(runbooks_spec, organization_id):
    connector = RunbooksConnector()
    resolved = _build_config(organization_id)
    client = await connector.authenticate(resolved)
    assert client is not None
    assert client.organization_id == organization_id
    await connector.close(client)
    print("PASS: Runbooks authenticate() succeeded (no network call -- see this file's module docstring)")


@pytest.mark.asyncio
async def test_fetch_and_normalize_real_postmortems(runbooks_spec, organization_id):
    connector = RunbooksConnector()
    resolved = _build_config(organization_id)
    client = await connector.authenticate(resolved)
    try:
        result = await connector.fetch_batch(client, since=None, cursor=None)
        assert isinstance(result.items, list)
        print(f"Fetched {len(result.items)} real postmortem(s) for this organization. has_more={result.has_more}")

        if not result.items:
            pytest.skip(
                "The bootstrapped test organization has zero approved/published postmortems yet -- "
                "nothing to normalize. Not a connector failure. Create a real incident + postmortem "
                "in this organization first (e.g. via the REST API) to exercise this test fully."
            )

        for raw_item in result.items:
            doc = connector.normalize(raw_item)
            assert isinstance(doc, RawDocument)
            assert doc.source == "runbooks"
            assert doc.external_id
            assert doc.content
            assert "incident_id" in doc.metadata
            assert "status" in doc.metadata
            assert "generated_by" in doc.metadata
        print(f"PASS: normalized {len(result.items)} real postmortem(s) into well-formed RawDocuments")
    finally:
        await connector.close(client)


@pytest.mark.asyncio
async def test_pagination_next_page_is_fetchable(runbooks_spec, organization_id):
    connector = RunbooksConnector()
    resolved = _build_config(organization_id)
    client = await connector.authenticate(resolved)
    try:
        first_page = await connector.fetch_batch(client, since=None, cursor=None)
        if not first_page.has_more:
            pytest.skip(
                "Fewer than 50 postmortems exist for this organization -- pagination path not "
                "exercised by this data. Not a failure."
            )
        second_page = await connector.fetch_batch(client, since=None, cursor=first_page.next_cursor)
        assert isinstance(second_page.items, list)
        print(f"PASS: fetched a real second page via next_cursor ({len(second_page.items)} item(s))")
    finally:
        await connector.close(client)


@pytest.mark.asyncio
async def test_since_filter_call_succeeds(runbooks_spec, organization_id):
    """See module docstring, point 4 -- this only confirms the call
    succeeds and is not larger than the unfiltered result; it does not
    independently re-verify `list_postmortems_for_ingestion`'s own SQL
    filter correctness."""
    from datetime import datetime, timezone

    connector = RunbooksConnector()
    resolved = _build_config(organization_id)
    client = await connector.authenticate(resolved)
    try:
        unfiltered = await connector.fetch_batch(client, since=None, cursor=None)
        very_recent = await connector.fetch_batch(client, since=datetime.now(timezone.utc), cursor=None)
        assert len(very_recent.items) <= len(unfiltered.items)
        print(
            f"PASS: since=now() returned {len(very_recent.items)} postmortem(s) vs "
            f"{len(unfiltered.items)} unfiltered"
        )
    finally:
        await connector.close(client)
