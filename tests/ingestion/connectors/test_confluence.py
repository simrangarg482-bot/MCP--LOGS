"""Tests for `app.ingestion.connectors.confluence` -- same `_FakeHttpClient`/
`_FakeResponse` style as `test_jira.py`. Tests construct `_ConfluenceClient`
directly (bypassing `authenticate`, which does a real `GET space` network
call) since `fetch_batch`/`normalize` only ever receive that object back.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from app.ingestion.connectors import confluence as confluence_module
from app.ingestion.connectors.confluence import ConfluenceConnector, _ConfluenceClient
from app.ingestion.schemas import ResolvedConnectorConfig
from app.ingestion.url_safety import UnsafeConnectorUrlError


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload

    @property
    def content(self) -> bytes:
        return self._payload


class _FakeHttpClient:
    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.get_urls: list[str] = []

    async def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        follow_redirects: bool = False,
    ) -> _FakeResponse:
        params = params or {}
        self.requests.append((url, params))
        self.get_urls.append(url)
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
    content_type: str = "page",
    container_id: str | None = None,
) -> dict[str, Any]:
    page: dict[str, Any] = {
        "id": content_id,
        "type": content_type,
        "title": title,
        "version": {"when": "2026-07-15T10:00:00.000Z", "number": version_number},
    }
    if body is not None:
        page["body"] = {"storage": {"value": body}}
    if author is not None:
        page["version"]["by"] = {"displayName": author}
    if webui is not None:
        page["_links"] = {"webui": webui}
    if container_id is not None:
        page["container"] = {"id": container_id}
    return page


def _attachment(
    content_id: str = "att-1",
    *,
    title: str = "runbook.pdf",
    download: str | None = "/download/attachments/12345/runbook.pdf",
) -> dict[str, Any]:
    attachment: dict[str, Any] = {"id": content_id, "type": "attachment", "title": title}
    if download is not None:
        attachment["_links"] = {"download": download}
    return attachment


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
        "kind": "page",
        "updated": "2026-07-15T10:00:00.000Z",
        "version": "3",
        "author": "Jane Doe",
    }


def test_normalize_blogpost_sets_kind_metadata() -> None:
    connector = ConfluenceConnector()
    raw_item = _page(content_type="blogpost")
    raw_item["_space_key"] = "ENG"
    raw_item["_base_url"] = "https://acme.atlassian.net"

    doc = connector.normalize(raw_item)

    assert doc.metadata["kind"] == "blogpost"


def test_normalize_comment_sets_kind_and_parent_id() -> None:
    connector = ConfluenceConnector()
    raw_item = _page(content_type="comment", container_id="12345")
    raw_item["_space_key"] = "ENG"
    raw_item["_base_url"] = "https://acme.atlassian.net"

    doc = connector.normalize(raw_item)

    assert doc.metadata["kind"] == "comment"
    assert doc.metadata["parent_id"] == "12345"


def test_normalize_page_has_no_parent_id() -> None:
    connector = ConfluenceConnector()
    raw_item = _page()
    raw_item["_space_key"] = "ENG"
    raw_item["_base_url"] = "https://acme.atlassian.net"

    doc = connector.normalize(raw_item)

    assert "parent_id" not in doc.metadata


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


def test_normalize_attachment_uses_extracted_content_over_body() -> None:
    connector = ConfluenceConnector()
    raw_item = _attachment()
    raw_item["_space_key"] = "ENG"
    raw_item["_base_url"] = "https://acme.atlassian.net"
    raw_item["_attachment_content"] = "Restart the checkout service."

    doc = connector.normalize(raw_item)

    assert doc.content == "Restart the checkout service."
    assert doc.metadata["kind"] == "attachment"
    assert doc.title == "runbook.pdf"


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

    assert 'space = "ENG" AND type in ("page","blogpost","comment","attachment")' in captured["cql"]
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


# --- fetch_batch: attachments --------------------------------------------


@pytest.mark.asyncio
async def test_fetch_batch_downloads_and_extracts_attachment_content(monkeypatch) -> None:
    connector = ConfluenceConnector()
    attachment = _attachment()
    payload = {"results": [attachment]}
    download_url = "https://acme.atlassian.net/wiki/download/attachments/12345/runbook.pdf"
    client = _client(
        ["ENG"], **{"content/search": payload, download_url: b"%PDF-fake-bytes"}
    )
    captured: dict[str, Any] = {}

    def fake_extract_text(filename: str, raw_bytes: bytes) -> str | None:
        captured["filename"] = filename
        captured["raw_bytes"] = raw_bytes
        return "Restart the checkout service."

    monkeypatch.setattr(confluence_module, "extract_text", fake_extract_text)

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert download_url in client.http.get_urls
    assert captured["filename"] == "runbook.pdf"
    assert captured["raw_bytes"] == b"%PDF-fake-bytes"
    assert result.items[0]["_attachment_content"] == "Restart the checkout service."


@pytest.mark.asyncio
async def test_fetch_batch_skips_attachment_without_download_link() -> None:
    connector = ConfluenceConnector()
    attachment = _attachment(download=None)
    payload = {"results": [attachment]}
    client = _client(["ENG"], **{"content/search": payload})

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.items == []


@pytest.mark.asyncio
async def test_fetch_batch_skips_attachment_when_extraction_fails(monkeypatch) -> None:
    connector = ConfluenceConnector()
    attachment = _attachment()
    payload = {"results": [attachment]}
    download_url = "https://acme.atlassian.net/wiki/download/attachments/12345/runbook.pdf"
    client = _client(
        ["ENG"], **{"content/search": payload, download_url: b"not-a-real-pdf"}
    )
    monkeypatch.setattr(confluence_module, "extract_text", lambda filename, raw_bytes: None)

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.items == []


@pytest.mark.asyncio
async def test_fetch_batch_pagination_uses_raw_count_not_filtered_count() -> None:
    """Regression test: an attachment skipped for lacking a download link
    must not shrink the exhaustion/`start`-advancement math -- both are
    offsets into Confluence's own (unfiltered) result set.
    """
    connector = ConfluenceConnector()
    page_items = [_page(str(i)) for i in range(24)] + [_attachment(download=None)]
    assert len(page_items) == 25  # a full page, per _SEARCH_PAGE_SIZE
    payload = {"results": page_items}
    client = _client(["ENG"], **{"content/search": payload})

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert len(result.items) == 24  # the download-link-less attachment was skipped
    assert result.has_more is True
    next_state = json.loads(result.next_cursor)
    assert next_state == {"space_index": 0, "start": 25}


def test_decode_cursor_defaults_to_first_space() -> None:
    assert ConfluenceConnector._decode_cursor(None) == (0, 0)


def test_decode_cursor_parses_envelope() -> None:
    cursor = json.dumps({"space_index": 2, "start": 50})
    assert ConfluenceConnector._decode_cursor(cursor) == (2, 50)


@pytest.mark.asyncio
async def test_authenticate_rejects_an_ssrf_base_url_before_any_network_call() -> None:
    """Phase 3 production-hardening regression -- see the matching test in
    test_jira.py for the full rationale.
    """
    config = ResolvedConnectorConfig(
        connector_config_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        project_id=None,
        source="confluence",
        credential_ref="someone@example.com:fake-token",
        config={"base_url": "http://127.0.0.1:8000", "spaces": ["ENG"]},
    )
    connector = ConfluenceConnector()

    with pytest.raises(UnsafeConnectorUrlError):
        await connector.authenticate(config)
