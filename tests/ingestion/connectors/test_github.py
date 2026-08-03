"""Tests for the GitHub connector extension (files unchanged + new commits/
pull requests/issues support) -- `app.ingestion.connectors.github`.

No real network access and no mocking dependency this project doesn't
already have (e.g. `respx`) is used: `_FakeHttpClient` below is a minimal
stand-in for `httpx.AsyncClient`, scripted per test with canned JSON
responses keyed by the exact relative URL the connector requests. Tests
construct `_GitHubClient`/`_RepoConfig` directly (bypassing `authenticate`,
which does a real `GET /rate_limit` network call) since `fetch_batch`/
`normalize` only ever receive that object back.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app.ingestion.connectors.github import GitHubConnector, _GitHubClient, _RepoConfig


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _FakeHttpClient:
    """`responses[url]` is either a fixed JSON payload or a callable taking
    the request's `params` dict and returning a JSON payload (for endpoints
    whose response depends on pagination/`since`).
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, params: dict[str, Any] | None = None) -> _FakeResponse:
        params = params or {}
        self.requests.append((url, params))
        value = self._responses[url]
        payload = value(params) if callable(value) else value
        return _FakeResponse(payload)


def _client(repo: str = "acme/widgets", ref: str = "main", **responses: Any) -> _GitHubClient:
    return _GitHubClient(http=_FakeHttpClient(responses), repos=[_RepoConfig(repo=repo, ref=ref)])


# --- file kind: regression, behavior must be unchanged ----------------------


def test_normalize_file_matches_pre_extension_shape() -> None:
    connector = GitHubConnector()
    raw_item = {
        "_kind": "file",
        "_repo": "acme/widgets",
        "_ref": "main",
        "path": "src/app.py",
        "content": "print('hello')",
    }

    doc = connector.normalize(raw_item)

    assert doc.source == "github"
    assert doc.external_id == "acme/widgets:src/app.py"
    assert doc.content == "print('hello')"
    assert doc.title == "src/app.py"
    assert doc.source_url == "https://github.com/acme/widgets/blob/main/src/app.py"
    assert doc.metadata == {"repo": "acme/widgets", "path": "src/app.py", "ref": "main"}


@pytest.mark.asyncio
async def test_fetch_batch_full_sync_files_then_advances_to_commits_phase() -> None:
    connector = GitHubConnector()
    tree_payload = {
        "tree": [
            {"type": "blob", "path": "README.md"},
            {"type": "blob", "path": "src/app.py"},
            {"type": "tree", "path": "src"},  # directories are not files, must be skipped
        ]
    }
    contents = {
        "repos/acme/widgets/contents/README.md": {"encoding": "base64", "content": _b64("hi")},
        "repos/acme/widgets/contents/src/app.py": {"encoding": "base64", "content": _b64("code")},
    }
    client = _client(
        **{"repos/acme/widgets/git/trees/main": tree_payload, **contents},
    )

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert {item["path"] for item in result.items} == {"README.md", "src/app.py"}
    assert all(item["_kind"] == "file" for item in result.items)
    assert result.has_more is True
    next_state = json.loads(result.next_cursor)
    assert next_state == {"repo_index": 0, "phase": "commits", "page": 0}


@pytest.mark.asyncio
async def test_fetch_batch_no_more_repos_returns_empty() -> None:
    connector = GitHubConnector()
    client = _GitHubClient(http=_FakeHttpClient({}), repos=[])

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.items == []
    assert result.has_more is False
    assert result.next_cursor is None


# --- commits -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_batch_commits_phase_fetches_detail_and_normalizes() -> None:
    connector = GitHubConnector()
    commits_list = [{"sha": "abc123"}]
    commit_detail = {
        "commit": {
            "message": "Fix null pointer in checkout\n\nLonger body here.",
            "author": {"name": "Ada Lovelace", "date": "2026-07-01T10:00:00Z"},
        },
        "files": [{"filename": "src/checkout.py"}, {"filename": "vendor/lib.lock"}],
    }
    client = _client(
        **{
            "repos/acme/widgets/commits": commits_list,
            "repos/acme/widgets/commits/abc123": commit_detail,
        }
    )
    cursor = json.dumps({"repo_index": 0, "phase": "commits", "page": 0})

    result = await connector.fetch_batch(client, since=None, cursor=cursor)

    assert len(result.items) == 1
    raw_item = result.items[0]
    assert raw_item["_kind"] == "commit"
    assert raw_item["sha"] == "abc123"
    assert raw_item["author"] == "Ada Lovelace"
    assert raw_item["changed_files"] == "src/checkout.py"  # .lock skipped

    doc = connector.normalize(raw_item)
    assert doc.external_id == "acme/widgets@abc123"
    assert doc.content == commit_detail["commit"]["message"]
    assert doc.title == "Fix null pointer in checkout"
    assert doc.source_url == "https://github.com/acme/widgets/commit/abc123"
    assert doc.metadata["kind"] == "commit"
    assert doc.metadata["sha"] == "abc123"
    assert doc.metadata["author"] == "Ada Lovelace"
    assert doc.metadata["timestamp"] == "2026-07-01T10:00:00Z"
    assert doc.metadata["changed_files"] == "src/checkout.py"

    # A short page (1 commit < page size) exhausts this phase and advances.
    next_state = json.loads(result.next_cursor)
    assert next_state == {"repo_index": 0, "phase": "pulls", "page": 0}


@pytest.mark.asyncio
async def test_list_commits_page_passes_since_to_api() -> None:
    connector = GitHubConnector()
    seen_params: dict[str, Any] = {}

    def commits_endpoint(params: dict[str, Any]) -> list[Any]:
        seen_params.update(params)
        return []

    client = _client(**{"repos/acme/widgets/commits": commits_endpoint})
    since = datetime(2026, 6, 1, tzinfo=timezone.utc)

    items, exhausted = await connector._list_commits_page(client.http, client.repos[0], since, 0)

    assert items == []
    assert exhausted is True
    assert seen_params["since"] == "2026-06-01T00:00:00+00:00"
    assert seen_params["page"] == 1  # GitHub pages are 1-indexed


# --- pull requests -------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_pull_requests_page_incremental_cutoff() -> None:
    connector = GitHubConnector()
    pulls_page = [
        {
            "number": 42,
            "title": "Add retry logic",
            "body": "Retries flaky calls.",
            "user": {"login": "grace"},
            "html_url": "https://github.com/acme/widgets/pull/42",
            "created_at": "2026-07-15T00:00:00Z",
            "updated_at": "2026-07-15T00:00:00Z",
        },
        {
            "number": 41,
            "title": "Old PR",
            "body": "Stale.",
            "user": {"login": "grace"},
            "html_url": "https://github.com/acme/widgets/pull/41",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",  # older than `since` below
        },
    ]
    client = _client(
        **{
            "repos/acme/widgets/pulls": pulls_page,
            "repos/acme/widgets/pulls/42": {"merged_at": "2026-07-16T00:00:00Z"},
            "repos/acme/widgets/pulls/42/files": [{"filename": "src/retry.py"}],
            "repos/acme/widgets/pulls/42/reviews": [{"user": {"login": "bob"}, "state": "APPROVED"}],
        }
    )
    since = datetime(2026, 7, 1, tzinfo=timezone.utc)

    items, exhausted = await connector._list_pull_requests_page(client.http, client.repos[0], since, 0)

    assert len(items) == 1  # PR #41 excluded -- older than `since`
    assert items[0]["number"] == 42
    assert items[0]["merged_at"] == "2026-07-16T00:00:00Z"
    assert items[0]["changed_files"] == "src/retry.py"
    assert items[0]["reviews"] == "bob:APPROVED"
    assert exhausted is True  # cutoff reached mid-page


def test_normalize_pull_request_embeds_title_in_content() -> None:
    connector = GitHubConnector()
    raw_item = {
        "_kind": "pull_request",
        "_repo": "acme/widgets",
        "_ref": "main",
        "number": 42,
        "title": "Add retry logic",
        "body": "Retries flaky calls.",
        "author": "grace",
        "html_url": "https://github.com/acme/widgets/pull/42",
        "created_at": "2026-07-15T00:00:00Z",
        "merged_at": "",
        "changed_files": "src/retry.py",
        "reviews": "",
    }

    doc = connector.normalize(raw_item)

    assert doc.external_id == "acme/widgets#pull-42"
    assert doc.content == "Add retry logic\n\nRetries flaky calls."
    assert doc.title == "Add retry logic"
    assert doc.metadata["kind"] == "pull_request"
    assert doc.metadata["number"] == "42"
    assert "merged_at" not in doc.metadata  # not merged -- key omitted, not empty-string
    assert "reviews" not in doc.metadata


# --- issues --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_issues_page_excludes_pull_requests() -> None:
    connector = GitHubConnector()
    issues_payload = [
        {
            "number": 7,
            "title": "Login fails after SSO redirect",
            "body": "Users see a blank page.",
            "user": {"login": "alice"},
            "html_url": "https://github.com/acme/widgets/issues/7",
            "labels": [{"name": "bug"}],
            "created_at": "2026-07-01T00:00:00Z",
            "closed_at": None,
            "comments": 1,
        },
        {
            "number": 8,
            "pull_request": {"url": "..."},  # a PR -- must be filtered out
            "title": "A PR, not an issue",
        },
    ]
    client = _client(
        **{
            "repos/acme/widgets/issues": issues_payload,
            "repos/acme/widgets/issues/7/comments": [
                {"user": {"login": "bob"}, "body": "Same here."}
            ],
        }
    )

    items, exhausted = await connector._list_issues_page(client.http, client.repos[0], None, 0)

    assert len(items) == 1
    assert items[0]["number"] == 7
    assert items[0]["labels"] == "bug"
    assert items[0]["comments_text"] == "bob: Same here."


def test_normalize_issue_appends_comments_after_delimiter() -> None:
    connector = GitHubConnector()
    raw_item = {
        "_kind": "issue",
        "_repo": "acme/widgets",
        "_ref": "main",
        "number": 7,
        "title": "Login fails after SSO redirect",
        "body": "Users see a blank page.",
        "author": "alice",
        "html_url": "https://github.com/acme/widgets/issues/7",
        "labels": "bug",
        "created_at": "2026-07-01T00:00:00Z",
        "closed_at": "",
        "comments_count": "1",
        "comments_text": "bob: Same here.",
    }

    doc = connector.normalize(raw_item)

    assert doc.external_id == "acme/widgets#issue-7"
    assert doc.content.startswith("Login fails after SSO redirect\n\nUsers see a blank page.")
    assert "--- Comments ---" in doc.content
    assert doc.content.endswith("bob: Same here.")
    assert doc.metadata["kind"] == "issue"
    assert doc.metadata["labels"] == "bug"
    assert "closed_at" not in doc.metadata


def test_normalize_unknown_kind_raises() -> None:
    connector = GitHubConnector()
    with pytest.raises(ValueError, match="Unknown GitHub raw item kind"):
        connector.normalize({"_kind": "something_else"})


def _b64(text: str) -> str:
    import base64

    return base64.b64encode(text.encode("utf-8")).decode("ascii")
