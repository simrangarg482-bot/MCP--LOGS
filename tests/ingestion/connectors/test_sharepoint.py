"""Tests for `app.ingestion.connectors.sharepoint` -- same `_FakeHttpClient`
style as `test_teams.py`, with responses that can be either a JSON payload
(delta listing calls) or a plain string (file-content download calls) --
`_FakeResponse` serves both via `.json()`/`.text` on the same object, since
which one a given test cares about depends only on which call it represents.
Tests construct `_SharePointClient` directly (bypassing `authenticate`,
which does a real `GET me` network call) since `fetch_batch`/`normalize`
only ever receive that object back.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app.ingestion.connectors.sharepoint import SharePointConnector, _SharePointClient


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload

    @property
    def text(self) -> str:
        return self._payload


class _FakeHttpClient:
    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.requests: list[str] = []

    async def get(self, url: str, params: dict[str, Any] | None = None) -> _FakeResponse:
        self.requests.append(url)
        value = self._responses[url]
        payload = value() if callable(value) else value
        return _FakeResponse(payload)


def _client(site_ids: list[str], **responses: Any) -> _SharePointClient:
    return _SharePointClient(http=_FakeHttpClient(responses), site_ids=site_ids)


def _file_entry(
    item_id: str,
    name: str = "runbook.md",
    *,
    download_url: str | None = "https://download.example.com/runbook.md",
    last_modified: str | None = "2026-07-15T10:00:00Z",
    web_url: str | None = "https://acme.sharepoint.com/sites/eng/runbook.md",
    folder_path: str | None = "/drives/b!abc/root:/Docs",
) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": item_id, "name": name, "file": {}}
    if download_url is not None:
        entry["@microsoft.graph.downloadUrl"] = download_url
    if last_modified is not None:
        entry["lastModifiedDateTime"] = last_modified
    if web_url is not None:
        entry["webUrl"] = web_url
    if folder_path is not None:
        entry["parentReference"] = {"path": folder_path}
    return entry


def _folder_entry(item_id: str, name: str = "Docs") -> dict[str, Any]:
    return {"id": item_id, "name": name, "folder": {}}


# --- normalize ---------------------------------------------------------------


def test_normalize_full_item() -> None:
    connector = SharePointConnector()
    raw_item = _file_entry("item-1")
    raw_item["_site_id"] = "site-1"
    raw_item["_content"] = "Restart the checkout service."

    doc = connector.normalize(raw_item)

    assert doc.source == "sharepoint"
    assert doc.external_id == "site-1:item-1"
    assert doc.title == "runbook.md"
    assert doc.content == "Restart the checkout service."
    assert doc.source_url == "https://acme.sharepoint.com/sites/eng/runbook.md"
    assert doc.metadata == {
        "site_id": "site-1",
        "updated": "2026-07-15T10:00:00Z",
        "folder_path": "/drives/b!abc/root:/Docs",
    }


def test_normalize_item_without_folder_or_weburl() -> None:
    connector = SharePointConnector()
    raw_item = _file_entry("item-2", web_url=None, folder_path=None)
    raw_item["_site_id"] = "site-1"
    raw_item["_content"] = "content"

    doc = connector.normalize(raw_item)

    assert doc.source_url is None
    assert "folder_path" not in doc.metadata


# --- fetch_batch ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_batch_skips_folders_and_unsupported_extensions() -> None:
    connector = SharePointConnector()
    supported = _file_entry("item-1", name="runbook.md")
    unsupported = _file_entry(
        "item-2", name="report.docx", download_url="https://download.example.com/report.docx"
    )
    payload = {"value": [_folder_entry("folder-1"), supported, unsupported]}
    client = _client(
        ["site-1"],
        **{
            "sites/site-1/drive/root/delta": payload,
            "https://download.example.com/runbook.md": "Restart the checkout service.",
        },
    )

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert [item["id"] for item in result.items] == ["item-1"]
    assert result.items[0]["_content"] == "Restart the checkout service."
    assert result.items[0]["_site_id"] == "site-1"


@pytest.mark.asyncio
async def test_fetch_batch_skips_file_missing_download_url() -> None:
    connector = SharePointConnector()
    entry = _file_entry("item-1", download_url=None)
    payload = {"value": [entry]}
    client = _client(["site-1"], **{"sites/site-1/drive/root/delta": payload})

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.items == []


@pytest.mark.asyncio
async def test_fetch_batch_filters_out_entries_older_than_since() -> None:
    connector = SharePointConnector()
    fresh = _file_entry("item-fresh", name="fresh.md", last_modified="2026-07-20T00:00:00Z")
    stale = _file_entry(
        "item-stale",
        name="stale.md",
        download_url="https://download.example.com/stale.md",
        last_modified="2026-07-01T00:00:00Z",
    )
    payload = {"value": [fresh, stale]}
    client = _client(
        ["site-1"],
        **{
            "sites/site-1/drive/root/delta": payload,
            "https://download.example.com/runbook.md": "fresh content",
        },
    )
    since = datetime(2026, 7, 15, tzinfo=timezone.utc)

    result = await connector.fetch_batch(client, since=since, cursor=None)

    assert [item["id"] for item in result.items] == ["item-fresh"]


@pytest.mark.asyncio
async def test_fetch_batch_follows_next_link_within_site() -> None:
    connector = SharePointConnector()
    next_link = "https://graph.microsoft.com/v1.0/sites/site-1/drive/root/delta?token=abc"
    entry = _file_entry("item-1")
    payload = {"value": [entry], "@odata.nextLink": next_link}
    client = _client(
        ["site-1"],
        **{
            "sites/site-1/drive/root/delta": payload,
            "https://download.example.com/runbook.md": "content",
        },
    )

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.has_more is True
    next_state = json.loads(result.next_cursor)
    assert next_state == {"site_index": 0, "next_link": next_link}


@pytest.mark.asyncio
async def test_fetch_batch_no_next_link_advances_to_next_site() -> None:
    connector = SharePointConnector()
    entry = _file_entry("item-1")
    payload = {"value": [entry], "@odata.deltaLink": "https://example.com/delta-token"}
    client = _client(
        ["site-1", "site-2"],
        **{
            "sites/site-1/drive/root/delta": payload,
            "https://download.example.com/runbook.md": "content",
        },
    )

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.has_more is True
    next_state = json.loads(result.next_cursor)
    assert next_state == {"site_index": 1, "next_link": None}


@pytest.mark.asyncio
async def test_fetch_batch_no_more_sites_returns_empty() -> None:
    connector = SharePointConnector()
    client = _SharePointClient(http=_FakeHttpClient({}), site_ids=[])

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.items == []
    assert result.has_more is False
    assert result.next_cursor is None


@pytest.mark.asyncio
async def test_fetch_batch_resumes_from_next_link_cursor() -> None:
    connector = SharePointConnector()
    next_link = "https://graph.microsoft.com/v1.0/sites/site-1/drive/root/delta?token=abc"
    entry = _file_entry("item-2")
    payload = {"value": [entry]}
    client = _client(
        ["site-1"],
        **{next_link: payload, "https://download.example.com/runbook.md": "content"},
    )
    cursor = json.dumps({"site_index": 0, "next_link": next_link})

    result = await connector.fetch_batch(client, since=None, cursor=cursor)

    assert [item["id"] for item in result.items] == ["item-2"]


def test_decode_cursor_defaults_to_first_site() -> None:
    assert SharePointConnector._decode_cursor(None) == (0, None)


def test_decode_cursor_parses_envelope() -> None:
    cursor = json.dumps({"site_index": 1, "next_link": "https://example.com/next"})
    assert SharePointConnector._decode_cursor(cursor) == (1, "https://example.com/next")
