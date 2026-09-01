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

import httpx
import pytest

from app.ingestion.connectors.github import GitHubConnector, _GitHubClient, _RepoConfig


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    @property
    def text(self) -> str:
        return json.dumps(self._payload) if not isinstance(self._payload, str) else self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=httpx.Request("GET", "http://x"), response=self  # type: ignore[arg-type]
            )

    def json(self) -> Any:
        return self._payload


class _FakeHttpClient:
    """`responses[url]` is either a fixed JSON payload, a `_FakeResponse` (to
    script a non-200 status), or a callable taking the request's `params`
    dict and returning one of those (for endpoints whose response depends on
    pagination/`since`).
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, params: dict[str, Any] | None = None) -> _FakeResponse:
        params = params or {}
        self.requests.append((url, params))
        value = self._responses[url]
        result = value(params) if callable(value) else value
        return result if isinstance(result, _FakeResponse) else _FakeResponse(result)


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
            {"type": "blob", "path": ".venv_mac/lib/site-packages/httpx/api.py"},
            {"type": "blob", "path": "web/node_modules/react/index.js"},
            {"type": "blob", "path": "service/build/generated.py"},
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


def test_skip_filter_keeps_similarly_named_source_files() -> None:
    connector = GitHubConnector()

    assert connector._is_skipped("backend/.venv_mac/lib/site-packages/pkg/api.py") is True
    assert connector._is_skipped("frontend/node_modules/react/index.js") is True
    assert connector._is_skipped("service/dist/app.js") is True
    assert connector._is_skipped("src/environment.py") is False
    assert connector._is_skipped("src/build_tools/compiler.py") is False


@pytest.mark.asyncio
async def test_full_sync_skips_blobs_over_the_inline_size_limit() -> None:
    """A >1 MB file cannot be fetched inline via the Contents API -- filter it
    out at tree-list time so it never triggers the 403 that used to crash the
    whole sync (observed against a real 51 MB repo on Railway).
    """
    connector = GitHubConnector()
    tree_payload = {
        "tree": [
            {"type": "blob", "path": "README.md", "size": 42},
            {"type": "blob", "path": "data/model.bin", "size": 5_000_000},
            {"type": "blob", "path": "src/app.py", "size": 900_000},
        ]
    }
    client = _client(
        **{
            "repos/acme/widgets/git/trees/main": tree_payload,
            "repos/acme/widgets/contents/README.md": {"encoding": "base64", "content": _b64("hi")},
            "repos/acme/widgets/contents/src/app.py": {"encoding": "base64", "content": _b64("x")},
        },
    )

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert {item["path"] for item in result.items} == {"README.md", "src/app.py"}
    assert not any(url.endswith("model.bin") for url, _ in client.http.requests)


@pytest.mark.asyncio
async def test_fetch_file_content_skips_oversized_403_and_vanished_404() -> None:
    connector = GitHubConnector()
    repo = _RepoConfig(repo="acme/widgets", ref="main")

    too_large = _FakeResponse(
        {"message": "This API returns blobs up to 1 MB in size. The requested blob is too large"},
        status_code=403,
    )
    gone = _FakeResponse({"message": "Not Found"}, status_code=404)
    rate_limited = _FakeResponse({"message": "API rate limit exceeded"}, status_code=403)

    http = _FakeHttpClient(
        {
            "repos/acme/widgets/contents/big.json": too_large,
            "repos/acme/widgets/contents/deleted.py": gone,
            "repos/acme/widgets/contents/blocked.py": rate_limited,
        }
    )

    assert await connector._fetch_file_content(http, repo, "big.json") is None
    assert await connector._fetch_file_content(http, repo, "deleted.py") is None
    with pytest.raises(httpx.HTTPStatusError):
        await connector._fetch_file_content(http, repo, "blocked.py")


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
