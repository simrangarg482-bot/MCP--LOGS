"""Tests for `app.ingestion.connectors.confluence` -- same `_FakeHttpClient`/
`_FakeResponse` style as `test_jira.py`. Tests construct `_ConfluenceClient`
directly (bypassing `authenticate`, which does a real `GET space` network
call) since `fetch_batch`/`normalize` only ever receive that object back.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app.ingestion.connectors.confluence import ConfluenceConnector, _ConfluenceClient


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _FakeHttpClient:
    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, params: dict[str, Any] | None = None) -> _FakeResponse:
        params = params or {}
        self.requests.append((url, params))
        value = self._responses[url]
        payload = value(params) if callable(value) else value
        return _FakeResponse(payload)


def _client(spaces: list[str], **responses: Any) -> _ConfluenceClient:
    return _ConfluenceClient(
        http=_FakeHttpClient(responses), spaces=spaces, base_url="https://acme.atlassian.net"
    )


def _page(
    content_id: str = "12345",
    *,
    title: str = "Runbook: checkout outage",
    body: str | None = "<p>Restart the checkout service.</p>",
    version_number: int = 3,
    author: str | None = "Jane Doe",
    webui: str | None = "/spaces/ENG/pages/12345/Runbook",
) -> dict[str, Any]:
    page: dict[str, Any] = {
        "id": content_id,
        "title": title,
        "version": {"when": "2026-07-15T10:00:00.000Z", "number": version_number},
    }
    if body is not None:
        page["body"] = {"storage": {"value": body}}
    if author is not None:
        page["version"]["by"] = {"displayName": author}
    if webui is not None:
        page["_links"] = {"webui": webui}
    return page


# --- normalize ---------------------------------------------------------------


def test_normalize_full_page() -> None:
    connector = ConfluenceConnector()
    raw_item = _page()
    raw_item["_space_key"] = "ENG"
    raw_item["_base_url"] = "https://acme.atlassian.net"

    doc = connector.normalize(raw_item)

    assert doc.source == "confluence"
    assert doc.external_id == "ENG:12345"
    assert doc.title == "Runbook: checkout outage"
    assert doc.content == "<p>Restart the checkout service.</p>"
    assert doc.source_url == "https://acme.atlassian.net/wiki/spaces/ENG/pages/12345/Runbook"
    assert doc.metadata == {
        "space": "ENG",
        "updated": "2026-07-15T10:00:00.000Z",
        "version": "3",
        "author": "Jane Doe",
    }


def test_normalize_page_without_webui_link_yields_no_source_url() -> None:
    connector = ConfluenceConnector()
    raw_item = _page(webui=None)
    raw_item["_space_key"] = "ENG"
    raw_item["_base_url"] = "https://acme.atlassian.net"

    doc = connector.normalize(raw_item)

    assert doc.source_url is None


def test_normalize_page_without_body_yields_empty_content() -> None:
    connector = ConfluenceConnector()
    raw_item = _page(body=None)
    raw_item["_space_key"] = "ENG"
    raw_item["_base_url"] = "https://acme.atlassian.net"

    doc = connector.normalize(raw_item)

    assert doc.content == ""


# --- fetch_batch ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_batch_partial_page_exhausts_space() -> None:
    connector = ConfluenceConnector()
    payload = {"results": [_page("1"), _page("2")]}  # 2 < _SEARCH_PAGE_SIZE (25)
    client = _client(["ENG"], **{"content/search": payload})

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert [item["id"] for item in result.items] == ["1", "2"]
    assert all(item["_space_key"] == "ENG" for item in result.items)
    assert all(item["_base_url"] == "https://acme.atlassian.net" for item in result.items)
    assert result.has_more is False
    assert result.next_cursor is None


@pytest.mark.asyncio
async def test_fetch_batch_full_page_advances_start() -> None:
    connector = ConfluenceConnector()
    payload = {"results": [_page(str(i)) for i in range(25)]}  # exactly a full page
    client = _client(["ENG"], **{"content/search": payload})

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.has_more is True
    next_state = json.loads(result.next_cursor)
    assert next_state == {"space_index": 0, "start": 25}


@pytest.mark.asyncio
async def test_fetch_batch_space_exhausted_advances_to_next_space() -> None:
    connector = ConfluenceConnector()
    payload = {"results": [_page("1")]}
    client = _client(["ENG", "OPS"], **{"content/search": payload})

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.has_more is True
    next_state = json.loads(result.next_cursor)
    assert next_state == {"space_index": 1, "start": 0}


@pytest.mark.asyncio
async def test_fetch_batch_no_more_spaces_returns_empty() -> None:
    connector = ConfluenceConnector()
    client = _ConfluenceClient(
        http=_FakeHttpClient({}), spaces=[], base_url="https://acme.atlassian.net"
    )

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.items == []
    assert result.has_more is False
    assert result.next_cursor is None


@pytest.mark.asyncio
async def test_fetch_batch_includes_since_in_cql() -> None:
    connector = ConfluenceConnector()
    captured: dict[str, Any] = {}

    def fake_search(params: dict[str, Any]) -> dict[str, Any]:
        captured.update(params)
        return {"results": []}

    client = _client(["ENG"], **{"content/search": fake_search})
    since = datetime(2026, 7, 1, 12, 30, tzinfo=timezone.utc)

    await connector.fetch_batch(client, since=since, cursor=None)

    assert 'space = "ENG" AND type = "page"' in captured["cql"]
    assert 'lastmodified >= "2026/07/01 12:30"' in captured["cql"]
    assert "ORDER BY lastmodified ASC" in captured["cql"]


@pytest.mark.asyncio
async def test_fetch_batch_resumes_from_cursor() -> None:
    connector = ConfluenceConnector()
    captured: dict[str, Any] = {}

    def fake_search(params: dict[str, Any]) -> dict[str, Any]:
        captured.update(params)
        return {"results": []}

    client = _client(["ENG", "OPS"], **{"content/search": fake_search})
    cursor = json.dumps({"space_index": 1, "start": 25})

    result = await connector.fetch_batch(client, since=None, cursor=cursor)

    assert captured["start"] == 25
    assert 'space = "OPS"' in captured["cql"]
    assert result.has_more is False


def test_decode_cursor_defaults_to_first_space() -> None:
    assert ConfluenceConnector._decode_cursor(None) == (0, 0)


def test_decode_cursor_parses_envelope() -> None:
    cursor = json.dumps({"space_index": 2, "start": 50})
    assert ConfluenceConnector._decode_cursor(cursor) == (2, 50)
