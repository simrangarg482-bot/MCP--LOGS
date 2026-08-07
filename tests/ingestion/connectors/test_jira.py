"""Tests for `app.ingestion.connectors.jira` -- same `_FakeHttpClient`/
`_FakeResponse` style as `tests/ingestion/connectors/test_github.py` (no real
network access, no new mocking dependency). Tests construct `_JiraClient`
directly (bypassing `authenticate`, which does a real `GET myself` network
call) since `fetch_batch`/`normalize` only ever receive that object back.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app.ingestion.connectors.jira import JiraConnector, _JiraClient


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _FakeHttpClient:
    """`responses[url]` is either a fixed JSON payload or a callable taking
    the request's `params` dict and returning a JSON payload.
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.get_urls: list[str] = []

    async def get(self, url: str, params: dict[str, Any] | None = None) -> _FakeResponse:
        params = params or {}
        self.requests.append((url, params))
        self.get_urls.append(url)
        value = self._responses[url]
        payload = value(params) if callable(value) else value
        return _FakeResponse(payload)


def _client(projects: list[str], **responses: Any) -> _JiraClient:
    return _JiraClient(
        http=_FakeHttpClient(responses), projects=projects, base_url="https://acme.atlassian.net"
    )


def _issue(
    key: str,
    *,
    summary: str = "Checkout fails intermittently",
    description: str | None = "Users report random 500s at checkout.",
    issue_type: str = "Bug",
    status: str = "Open",
    assignee: str | None = "Jane Doe",
    reporter: str | None = "John Roe",
    comment_total: int | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "summary": summary,
        "description": description,
        "issuetype": {"name": issue_type},
        "status": {"name": status},
        "created": "2026-07-01T00:00:00.000+0000",
        "updated": "2026-07-15T00:00:00.000+0000",
    }
    if assignee is not None:
        fields["assignee"] = {"displayName": assignee}
    if reporter is not None:
        fields["reporter"] = {"displayName": reporter}
    if comment_total is not None:
        fields["comment"] = {"total": comment_total}
    return {
        "key": key,
        "self": f"https://acme.atlassian.net/rest/api/2/issue/{key}",
        "fields": fields,
    }


# --- normalize ---------------------------------------------------------------


def test_normalize_full_issue() -> None:
    connector = JiraConnector()
    raw_item = _issue("OPS-42")
    raw_item["_project_key"] = "OPS"

    doc = connector.normalize(raw_item)

    assert doc.source == "jira"
    assert doc.external_id == "OPS-42"
    assert doc.title == "Checkout fails intermittently"
    assert doc.content == (
        "Checkout fails intermittently\n\nUsers report random 500s at checkout."
    )
    assert doc.source_url == "https://acme.atlassian.net/browse/OPS-42"
    assert doc.metadata == {
        "project": "OPS",
        "issue_type": "Bug",
        "status": "Open",
        "assignee": "Jane Doe",
        "reporter": "John Roe",
        "created": "2026-07-01T00:00:00.000+0000",
        "updated": "2026-07-15T00:00:00.000+0000",
    }


def test_normalize_issue_without_description_uses_summary_only() -> None:
    connector = JiraConnector()
    raw_item = _issue("OPS-43", description=None, assignee=None, reporter=None)
    raw_item["_project_key"] = "OPS"

    doc = connector.normalize(raw_item)

    assert doc.content == "Checkout fails intermittently"
    assert "assignee" not in doc.metadata
    assert "reporter" not in doc.metadata


def test_normalize_missing_self_link_yields_no_source_url() -> None:
    connector = JiraConnector()
    raw_item = _issue("OPS-44")
    raw_item["_project_key"] = "OPS"
    del raw_item["self"]

    doc = connector.normalize(raw_item)

    assert doc.source_url is None


def test_normalize_issue_appends_comments_after_delimiter() -> None:
    connector = JiraConnector()
    raw_item = _issue("OPS-45", comment_total=2)
    raw_item["_project_key"] = "OPS"
    raw_item["_comments_text"] = "Jane Doe: Investigating.\n\nJohn Roe: Fixed by restart."

    doc = connector.normalize(raw_item)

    assert doc.content == (
        "Checkout fails intermittently\n\nUsers report random 500s at checkout."
        "\n\n--- Comments ---\n\nJane Doe: Investigating.\n\nJohn Roe: Fixed by restart."
    )
    assert doc.metadata["comments_count"] == "2"


def test_normalize_issue_without_comments_omits_delimiter_and_count() -> None:
    connector = JiraConnector()
    raw_item = _issue("OPS-46")
    raw_item["_project_key"] = "OPS"
    raw_item["_comments_text"] = ""

    doc = connector.normalize(raw_item)

    assert "--- Comments ---" not in doc.content
    assert "comments_count" not in doc.metadata


# --- fetch_batch ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_batch_single_page_exhausts_project() -> None:
    connector = JiraConnector()
    payload = {"issues": [_issue("OPS-1"), _issue("OPS-2")], "total": 2, "startAt": 0}
    client = _client(["OPS"], search=payload)

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert [item["key"] for item in result.items] == ["OPS-1", "OPS-2"]
    assert all(item["_project_key"] == "OPS" for item in result.items)
    assert result.has_more is False
    assert result.next_cursor is None


@pytest.mark.asyncio
async def test_fetch_batch_more_results_than_page_advances_start_at() -> None:
    connector = JiraConnector()
    payload = {"issues": [_issue("OPS-1")], "total": 5, "startAt": 0}
    client = _client(["OPS"], search=payload)

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.has_more is True
    next_state = json.loads(result.next_cursor)
    assert next_state == {"project_index": 0, "start_at": 1}


@pytest.mark.asyncio
async def test_fetch_batch_project_exhausted_advances_to_next_project() -> None:
    connector = JiraConnector()
    payload = {"issues": [_issue("OPS-1")], "total": 1, "startAt": 0}
    client = _client(["OPS", "ENG"], search=payload)

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.has_more is True
    next_state = json.loads(result.next_cursor)
    assert next_state == {"project_index": 1, "start_at": 0}


@pytest.mark.asyncio
async def test_fetch_batch_last_project_exhausted_ends_sync() -> None:
    connector = JiraConnector()
    payload = {"issues": [_issue("ENG-1")], "total": 1, "startAt": 0}
    client = _client(["ENG"], search=payload)

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.has_more is False
    assert result.next_cursor is None


@pytest.mark.asyncio
async def test_fetch_batch_no_more_projects_returns_empty() -> None:
    connector = JiraConnector()
    client = _JiraClient(
        http=_FakeHttpClient({}), projects=[], base_url="https://acme.atlassian.net"
    )

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.items == []
    assert result.has_more is False
    assert result.next_cursor is None


@pytest.mark.asyncio
async def test_fetch_batch_includes_since_in_jql() -> None:
    connector = JiraConnector()
    captured: dict[str, Any] = {}

    def fake_search(params: dict[str, Any]) -> dict[str, Any]:
        captured.update(params)
        return {"issues": [], "total": 0, "startAt": 0}

    client = _client(["OPS"], search=fake_search)
    since = datetime(2026, 7, 1, 12, 30, tzinfo=timezone.utc)

    await connector.fetch_batch(client, since=since, cursor=None)

    assert 'project = "OPS"' in captured["jql"]
    assert 'updated >= "2026-07-01 12:30"' in captured["jql"]
    assert "ORDER BY updated ASC" in captured["jql"]


@pytest.mark.asyncio
async def test_fetch_batch_resumes_from_cursor() -> None:
    connector = JiraConnector()
    captured: dict[str, Any] = {}

    def fake_search(params: dict[str, Any]) -> dict[str, Any]:
        captured.update(params)
        return {"issues": [], "total": 3, "startAt": 3}

    client = _client(["OPS", "ENG"], search=fake_search)
    cursor = json.dumps({"project_index": 1, "start_at": 3})

    result = await connector.fetch_batch(client, since=None, cursor=cursor)

    assert captured["startAt"] == 3
    assert 'project = "ENG"' in captured["jql"]
    assert result.has_more is False


@pytest.mark.asyncio
async def test_fetch_batch_fetches_comments_when_issue_has_any() -> None:
    connector = JiraConnector()
    payload = {"issues": [_issue("OPS-1", comment_total=2)], "total": 1, "startAt": 0}
    comments_payload = {
        "comments": [
            {"author": {"displayName": "Jane Doe"}, "body": "Investigating."},
            {"author": {"displayName": "John Roe"}, "body": "Fixed by restart."},
        ]
    }
    client = _client(
        ["OPS"], search=payload, **{"issue/OPS-1/comment": comments_payload}
    )

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert "issue/OPS-1/comment" in client.http.get_urls
    assert result.items[0]["_comments_text"] == (
        "Jane Doe: Investigating.\n\nJohn Roe: Fixed by restart."
    )


@pytest.mark.asyncio
async def test_fetch_batch_skips_comments_call_when_issue_has_none() -> None:
    connector = JiraConnector()
    payload = {"issues": [_issue("OPS-1", comment_total=0)], "total": 1, "startAt": 0}
    client = _client(["OPS"], search=payload)

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert "issue/OPS-1/comment" not in client.http.get_urls
    assert result.items[0]["_comments_text"] == ""


def test_decode_cursor_defaults_to_first_project() -> None:
    assert JiraConnector._decode_cursor(None) == (0, 0)


def test_decode_cursor_parses_envelope() -> None:
    cursor = json.dumps({"project_index": 2, "start_at": 50})
    assert JiraConnector._decode_cursor(cursor) == (2, 50)
