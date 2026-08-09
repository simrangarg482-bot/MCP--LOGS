"""Tests for `app.ingestion.connectors.sharepoint` -- same `_FakeHttpClient`
style as `test_teams.py`, with responses that can be a JSON payload (delta
listing calls), a plain string (plain-text file-content download calls), or
raw `bytes` (Office/PDF file-content download calls, which `_fetch_text_
content` reads via `.content`, not `.text`). `_FakeResponse` serves all
three via `.json()`/`.text`/`.content` on the same object, since which one a
given test cares about depends only on which call it represents. Tests
construct `_SharePointClient` directly (bypassing `authenticate`, which
does a real `GET me` network call) since `fetch_batch`/`normalize` only
ever receive that object back.

Office/PDF/Excel extraction itself is tested against the real parsing
libraries in `tests/ingestion/test_office_extraction.py`, not here -- the
tests below monkeypatch `app.ingestion.connectors.sharepoint.extract_text`
to confirm this connector calls it with the right filename/bytes and uses
the result, without re-verifying the parsing.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from app.ingestion.connectors import sharepoint as sharepoint_module
from app.ingestion.connectors.sharepoint import SharePointConnector, _SharePointClient
from app.ingestion.schemas import ResolvedConnectorConfig


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

    @property
    def content(self) -> bytes:
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
        "item-2", name="photo.jpg", download_url="https://download.example.com/photo.jpg"
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
async def test_fetch_batch_dispatches_office_extension_to_extract_text(monkeypatch) -> None:
    connector = SharePointConnector()
    entry = _file_entry(
        "item-1", name="runbook.docx", download_url="https://download.example.com/runbook.docx"
    )
    payload = {"value": [entry]}
    client = _client(
        ["site-1"],
        **{
            "sites/site-1/drive/root/delta": payload,
            "https://download.example.com/runbook.docx": b"docx-bytes",
        },
    )
    captured: dict[str, Any] = {}

    def fake_extract_text(filename: str, raw_bytes: bytes) -> str | None:
        captured["filename"] = filename
        captured["raw_bytes"] = raw_bytes
        return "Restart the checkout service."

    monkeypatch.setattr(sharepoint_module, "extract_text", fake_extract_text)

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert captured["filename"] == "runbook.docx"
    assert captured["raw_bytes"] == b"docx-bytes"
    assert result.items[0]["_content"] == "Restart the checkout service."


@pytest.mark.asyncio
async def test_fetch_batch_skips_file_when_extraction_fails(monkeypatch) -> None:
    connector = SharePointConnector()
    entry = _file_entry(
        "item-1", name="runbook.docx", download_url="https://download.example.com/runbook.docx"
    )
    payload = {"value": [entry]}
    client = _client(
        ["site-1"],
        **{
            "sites/site-1/drive/root/delta": payload,
            "https://download.example.com/runbook.docx": b"not a real docx file",
        },
    )
    monkeypatch.setattr(sharepoint_module, "extract_text", lambda filename, raw_bytes: None)

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.items == []


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
async def test_fetch_batch_captures_new_delta_link_as_resume_token() -> None:
    connector = SharePointConnector()
    entry = _file_entry("item-1")
    new_delta_link = "https://example.com/delta-token-new"
    payload = {"value": [entry], "@odata.deltaLink": new_delta_link}
    client = _client(
        ["site-1"],
        **{
            "sites/site-1/drive/root/delta": payload,
            "https://download.example.com/runbook.md": "content",
        },
    )

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.resume_token is not None
    assert json.loads(result.resume_token) == {"site-1": new_delta_link}


@pytest.mark.asyncio
async def test_fetch_batch_resumes_from_saved_delta_link_for_site() -> None:
    connector = SharePointConnector()
    saved_delta_link = "https://example.com/delta-token-old"
    entry = _file_entry("item-1")
    payload = {"value": [entry], "@odata.deltaLink": "https://example.com/delta-token-new"}
    client = _client(
        ["site-1"],
        **{saved_delta_link: payload, "https://download.example.com/runbook.md": "content"},
    )
    resume_token = json.dumps({"site-1": saved_delta_link})

    result = await connector.fetch_batch(
        client, since=None, cursor=None, resume_token=resume_token
    )

    # Resuming from a saved deltaLink means calling that URL directly --
    # no bare `sites/site-1/drive/root/delta` call should happen.
    assert client.http.requests[0] == saved_delta_link
    assert [item["id"] for item in result.items] == ["item-1"]


@pytest.mark.asyncio
async def test_fetch_batch_resume_state_survives_across_sites() -> None:
    connector = SharePointConnector()
    delta_link_1 = "https://example.com/delta-token-1"
    payload_1 = {"value": [_file_entry("item-1")], "@odata.deltaLink": delta_link_1}
    delta_link_2 = "https://example.com/delta-token-2"
    payload_2 = {"value": [_file_entry("item-2")], "@odata.deltaLink": delta_link_2}
    client = _client(
        ["site-1", "site-2"],
        **{
            "sites/site-1/drive/root/delta": payload_1,
            "sites/site-2/drive/root/delta": payload_2,
            "https://download.example.com/runbook.md": "content",
        },
    )

    first = await connector.fetch_batch(client, since=None, cursor=None)
    next_state = json.loads(first.next_cursor)
    assert next_state == {"site_index": 1, "next_link": None}

    second = await connector.fetch_batch(client, since=None, cursor=first.next_cursor)

    # Site 1's entry (learned on the first call) must still be present
    # even though this call only just learned site 2's.
    assert json.loads(second.resume_token) == {"site-1": delta_link_1, "site-2": delta_link_2}


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


# --- authenticate: the credential probe ----------------------------------
#
# Unit tests of WHICH endpoint the probe calls, with httpx faked -- they do
# NOT contact Microsoft Graph and are NOT evidence that a real token works.
# Live verification requires a real Graph token; see
# scripts/live_connector_tests/.


class _FakeAsyncClient:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.requested: list[str] = []
        self.closed = False

    async def get(self, url: str, params: dict[str, Any] | None = None) -> _FakeResponse:
        self.requested.append(url)
        return _FakeResponse({})

    async def aclose(self) -> None:
        self.closed = True


def _install_fake_httpx(monkeypatch) -> list[_FakeAsyncClient]:
    created: list[_FakeAsyncClient] = []

    def _factory(**kwargs: Any) -> _FakeAsyncClient:
        client = _FakeAsyncClient(**kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(sharepoint_module.httpx, "AsyncClient", _factory)
    return created


def _resolved_config(**config: Any) -> ResolvedConnectorConfig:
    return ResolvedConnectorConfig(
        connector_config_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        project_id=None,
        source="sharepoint",
        credential_ref="fake-graph-token",
        config=config,
    )


@pytest.mark.asyncio
async def test_authenticate_probes_first_configured_site_and_never_me(monkeypatch) -> None:
    """`/me` only resolves a signed-in user and is invalid for
    application-permission (client-credentials) tokens -- the credential
    type this connector's own docstring recommends. The probe must target a
    configured site instead.
    """
    created = _install_fake_httpx(monkeypatch)

    await SharePointConnector().authenticate(_resolved_config(site_ids=["site-1", "site-2"]))

    assert created[0].requested == ["sites/site-1"]
    assert "me" not in created[0].requested


@pytest.mark.asyncio
async def test_authenticate_uses_the_configured_site_id_verbatim(monkeypatch) -> None:
    created = _install_fake_httpx(monkeypatch)

    await SharePointConnector().authenticate(
        _resolved_config(site_ids=["contoso.sharepoint.com,guid-a,guid-b"])
    )

    assert created[0].requested == ["sites/contoso.sharepoint.com,guid-a,guid-b"]


@pytest.mark.asyncio
async def test_authenticate_requires_site_ids(monkeypatch) -> None:
    """With no sites configured there is nothing to probe and nothing this
    connector could ever fetch -- same treatment `TeamsConnector` gives a
    missing `team_id`.
    """
    _install_fake_httpx(monkeypatch)

    with pytest.raises(RuntimeError, match="site_ids"):
        await SharePointConnector().authenticate(_resolved_config())


def test_decode_cursor_defaults_to_first_site() -> None:
    assert SharePointConnector._decode_cursor(None) == (0, None)


def test_decode_cursor_parses_envelope() -> None:
    cursor = json.dumps({"site_index": 1, "next_link": "https://example.com/next"})
    assert SharePointConnector._decode_cursor(cursor) == (1, "https://example.com/next")
