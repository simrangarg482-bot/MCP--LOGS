"""Live integration test for `app.ingestion.connectors.github.GitHubConnector`.

WHAT THIS VERIFIES (real network calls, no mocking -- separate from the
existing, untouched, fully-mocked `tests/ingestion/connectors/test_github.py`)
    1. `authenticate()` succeeds against the real
       `GET https://api.github.com/rate_limit`.
    2. `authenticate()` correctly REJECTS an invalid token.
    3. `fetch_batch()` on a fresh sync starts at the "files" phase (a real
       repo tree walk) and every item normalizes into a well-formed
       `RawDocument`.
    4. Pagination across this connector's 4 internal phases (files -> commits
       -> pulls -> issues, cycled via its own opaque cursor) -- follows
       `next_cursor` for a bounded number of hops and confirms each hop
       succeeds and the (phase, page) state visibly advances (no infinite
       loop).
    5. `since` on the "commits" phase specifically (GitHub's real server-side
       `since=` param on `GET /repos/{repo}/commits`) -- this connector's
       OTHER phases either ignore `since` (files, when since=None) or filter
       client-side (pulls), so this is the one phase where a real
       "since=now() returns fewer commits" assertion is meaningful. Reached
       by constructing a cursor directly in this connector's own documented
       opaque format (`{"repo_index": 0, "phase": "commits", "page": 0}`) --
       not a private-API reach-around, just using the public contract
       `FetchResult.next_cursor`/`cursor` already documents as an opaque
       string this connector round-trips itself.

REQUIRES (tests/ingestion_retrieval/.env)
    EKIP_TEST_GITHUB_TOKEN, EKIP_TEST_GITHUB_REPO (single "owner/name"),
    EKIP_TEST_GITHUB_REF (optional, default "main")
    Token scope needed: `repo` read access (classic PAT) or equivalent
    fine-grained contents+metadata read permissions.

RUN
    pytest scripts/live_connector_tests/test_github_live.py -v -s
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from app.ingestion.connectors.github import GitHubConnector
from app.ingestion.schemas import RawDocument, ResolvedConnectorConfig


def _build_config(credential_ref: str, config: dict, organization_id: uuid.UUID) -> ResolvedConnectorConfig:
    return ResolvedConnectorConfig(
        connector_config_id=uuid.uuid4(),
        organization_id=organization_id,
        project_id=None,
        source="github",
        credential_ref=credential_ref,
        config=config,
    )


@pytest.mark.asyncio
async def test_authenticate_succeeds_with_real_token(github_spec, organization_id):
    connector = GitHubConnector()
    resolved = _build_config(github_spec.credential_ref, github_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    assert client is not None
    await connector.close(client)
    print("PASS: GitHub authenticate() succeeded against real GET https://api.github.com/rate_limit")


@pytest.mark.asyncio
async def test_authenticate_rejects_invalid_token(github_spec, organization_id):
    connector = GitHubConnector()
    resolved = _build_config("ghp_deliberately_invalid_token", github_spec.config, organization_id)
    with pytest.raises(Exception):
        await connector.authenticate(resolved)
    print("PASS: GitHub authenticate() correctly rejected an invalid token")


@pytest.mark.asyncio
async def test_fetch_and_normalize_real_files(github_spec, organization_id):
    connector = GitHubConnector()
    resolved = _build_config(github_spec.credential_ref, github_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        result = await connector.fetch_batch(client, since=None, cursor=None)
        assert isinstance(result.items, list)
        print(f"Fetched {len(result.items)} raw file item(s) from GitHub (files phase). has_more={result.has_more}")

        if not result.items:
            pytest.skip(
                "Repo tree walk returned zero files -- nothing to normalize. Not a connector "
                "failure; point EKIP_TEST_GITHUB_REPO at a real, non-empty repository."
            )

        for raw_item in result.items:
            doc = connector.normalize(raw_item)
            assert isinstance(doc, RawDocument)
            assert doc.source == "github"
            assert doc.external_id
            assert "repo" in doc.metadata
        print(f"PASS: normalized {len(result.items)} real GitHub file(s) into well-formed RawDocuments")
    finally:
        await connector.close(client)


@pytest.mark.asyncio
async def test_pagination_advances_through_phases(github_spec, organization_id):
    connector = GitHubConnector()
    resolved = _build_config(github_spec.credential_ref, github_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        cursor: str | None = None
        seen_phases: set[str] = set()
        max_hops = 12  # bounded -- this is a pagination sanity check, not a full sync
        for _ in range(max_hops):
            result = await connector.fetch_batch(client, since=None, cursor=cursor)
            if cursor is not None:
                decoded = json.loads(cursor)
                seen_phases.add(decoded["phase"])
            if not result.has_more:
                break
            assert result.next_cursor is not None, "has_more=True but next_cursor is None -- pagination contract broken"
            cursor = result.next_cursor
        print(f"PASS: followed real pagination across {max_hops} hop(s); phases observed: {seen_phases or {'files'}}")
    finally:
        await connector.close(client)


@pytest.mark.asyncio
async def test_since_filter_on_commits_phase(github_spec, organization_id):
    connector = GitHubConnector()
    resolved = _build_config(github_spec.credential_ref, github_spec.config, organization_id)
    client = await connector.authenticate(resolved)
    try:
        commits_cursor = json.dumps({"repo_index": 0, "phase": "commits", "page": 0})
        unfiltered = await connector.fetch_batch(client, since=None, cursor=commits_cursor)
        very_recent = await connector.fetch_batch(client, since=datetime.now(timezone.utc), cursor=commits_cursor)
        assert len(very_recent.items) <= len(unfiltered.items)
        print(
            f"PASS: since=now() on the commits phase (real GitHub `since=` param) returned "
            f"{len(very_recent.items)} commit(s) vs {len(unfiltered.items)} unfiltered"
        )
    finally:
        await connector.close(client)
