"""Tests for `app.agents.investigation.live.github_live.GitHubLiveSource`.

Uses the same minimal `_FakeHttpClient`-style stand-in for `httpx.AsyncClient`
as `tests/ingestion/connectors/test_github.py` -- no real network access, no
mocking dependency this project doesn't already have.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from app.agents.investigation.live.github_live import GitHubLiveSource
from app.core.tenancy.schemas import ConnectorConfig


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _FakeHttpClient:
    """`responses[url]` is either a fixed JSON payload or a callable taking
    the request's `params` dict and returning one.
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> "_FakeHttpClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def get(self, url: str, params: dict[str, Any] | None = None) -> _FakeResponse:
        params = params or {}
        self.requests.append((url, params))
        value = self._responses[url]
        payload = value(params) if callable(value) else value
        return _FakeResponse(payload)


def _connector_config(repos: list[dict[str, str]]) -> ConnectorConfig:
    now = datetime.now(timezone.utc)
    return ConnectorConfig(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        project_id=None,
        source="github",
        credential_ref="ghp_test_token",
        config={"repos": repos},
        status="active",
        last_synced_at=None,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_fetch_live_evidence_returns_empty_without_repos(monkeypatch) -> None:
    source = GitHubLiveSource()
    connector_config = _connector_config([])

    result = await source.fetch_live_evidence(
        connector_config=connector_config,
        query="checkout failing",
        since=datetime.now(timezone.utc),
        limit=5,
    )

    assert result == []


@pytest.mark.asyncio
async def test_fetch_live_evidence_builds_evidence_from_search_results(monkeypatch) -> None:
    source = GitHubLiveSource()
    connector_config = _connector_config([{"repo": "acme/widgets", "ref": "main"}])

    issues_payload = {
        "items": [
            {
                "number": 7,
                "title": "Checkout throws 500",
                "body": "Users see a blank page at checkout.",
                "html_url": "https://github.com/acme/widgets/issues/7",
                "user": {"login": "alice"},
                "updated_at": "2026-08-01T12:00:00Z",
            },
            {
                "number": 42,
                "pull_request": {"url": "..."},  # marks this as a PR, not a plain issue
                "title": "Fix checkout 500",
                "body": "Adds a null check.",
                "html_url": "https://github.com/acme/widgets/pull/42",
                "user": {"login": "bob"},
                "updated_at": "2026-08-02T09:00:00Z",
            },
        ]
    }
    commits_payload = {
        "items": [
            {
                "sha": "abc123",
                "html_url": "https://github.com/acme/widgets/commit/abc123",
                "commit": {
                    "message": "Fix null pointer in checkout",
                    "author": {"name": "Ada Lovelace", "date": "2026-08-02T08:00:00Z"},
                },
            }
        ]
    }

    fake_http = _FakeHttpClient(
        {"search/issues": issues_payload, "search/commits": commits_payload}
    )
    monkeypatch.setattr(
        "app.agents.investigation.live.github_live.httpx.AsyncClient",
        lambda *args, **kwargs: fake_http,
    )

    result = await source.fetch_live_evidence(
        connector_config=connector_config,
        query="checkout",
        since=datetime(2026, 7, 1, tzinfo=timezone.utc),
        limit=10,
    )

    sources_found = {item.source for item in result}
    assert sources_found == {"issue", "pull_request", "commit"}

    pr_item = next(item for item in result if item.source == "pull_request")
    assert pr_item.metadata["retrieval_mode"] == "live"
    assert pr_item.metadata["kind"] == "pull_request"
    assert pr_item.reference == "https://github.com/acme/widgets/pull/42"

    commit_item = next(item for item in result if item.source == "commit")
    assert commit_item.metadata["sha"] == "abc123"
    assert commit_item.source_timestamp == datetime(2026, 8, 2, 8, 0, 0, tzinfo=timezone.utc)

    # Sorted newest-first: PR (08-02 09:00) > commit (08-02 08:00) > issue (08-01 12:00).
    assert [item.source for item in result] == ["pull_request", "commit", "issue"]


@pytest.mark.asyncio
async def test_fetch_live_evidence_respects_limit_and_repo_cap(monkeypatch) -> None:
    source = GitHubLiveSource()
    many_repos = [{"repo": f"acme/repo{i}", "ref": "main"} for i in range(10)]
    connector_config = _connector_config(many_repos)

    call_count = {"n": 0}

    def empty_payload(_params: dict[str, Any]) -> dict[str, Any]:
        call_count["n"] += 1
        return {"items": []}

    fake_http = _FakeHttpClient({"search/issues": empty_payload, "search/commits": empty_payload})
    monkeypatch.setattr(
        "app.agents.investigation.live.github_live.httpx.AsyncClient",
        lambda *args, **kwargs: fake_http,
    )

    result = await source.fetch_live_evidence(
        connector_config=connector_config,
        query="q",
        since=datetime(2026, 7, 1, tzinfo=timezone.utc),
        limit=3,
    )

    assert result == []
    # Capped at _MAX_REPOS_PER_CALL (5) repos x 2 endpoints each = 10 calls,
    # not 10 repos x 2 = 20.
    assert call_count["n"] == 10


@pytest.mark.asyncio
async def test_fetch_live_evidence_logs_and_skips_failed_endpoint(monkeypatch) -> None:
    source = GitHubLiveSource()
    connector_config = _connector_config([{"repo": "acme/widgets", "ref": "main"}])

    class _RaisingHttpClient(_FakeHttpClient):
        async def get(self, url: str, params: dict[str, Any] | None = None) -> _FakeResponse:
            raise RuntimeError("boom")

    fake_http = _RaisingHttpClient({})
    monkeypatch.setattr(
        "app.agents.investigation.live.github_live.httpx.AsyncClient",
        lambda *args, **kwargs: fake_http,
    )

    result = await source.fetch_live_evidence(
        connector_config=connector_config,
        query="q",
        since=datetime(2026, 7, 1, tzinfo=timezone.utc),
        limit=5,
    )

    assert result == []
