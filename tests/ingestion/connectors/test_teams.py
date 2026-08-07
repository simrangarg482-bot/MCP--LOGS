"""Tests for `app.ingestion.connectors.teams` -- same `_FakeHttpClient`/
`_FakeResponse` style as `test_github.py`/`test_jira.py`. Tests construct
`_TeamsClient` directly (bypassing `authenticate`, which does a real `GET
me` network call) since `fetch_batch`/`normalize` only ever receive that
object back.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app.ingestion.connectors.teams import TeamsConnector, _TeamsClient


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

    async def get(self, url: str, params: dict[str, Any] | None = None) -> _FakeResponse:
        params = params or {}
        self.requests.append((url, params))
        value = self._responses[url]
        payload = value(params) if callable(value) else value
        return _FakeResponse(payload)


def _client(channels: list[str], team_id: str = "team-1", **responses: Any) -> _TeamsClient:
    return _TeamsClient(http=_FakeHttpClient(responses), team_id=team_id, channels=channels)


def _message(
    message_id: str = "msg-1",
    *,
    content: str = "<p>Checkout is failing again</p>",
    author: str | None = "Jane Doe",
    created: str = "2026-07-15T10:00:00Z",
    web_url: str | None = "https://teams.microsoft.com/l/message/19:abc/msg-1",
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "id": message_id,
        "body": {"content": content, "contentType": "html"},
        "createdDateTime": created,
    }
    if author is not None:
        message["from"] = {"user": {"displayName": author}}
    if web_url is not None:
        message["webUrl"] = web_url
    return message


# --- normalize ---------------------------------------------------------------


def test_normalize_full_message() -> None:
    connector = TeamsConnector()
    raw_item = _message()
    raw_item["_channel_id"] = "19:channel-1"

    doc = connector.normalize(raw_item)

    assert doc.source == "teams"
    assert doc.external_id == "19:channel-1:msg-1"
    assert doc.content == "<p>Checkout is failing again</p>"
    assert doc.title is None
    assert doc.source_url == "https://teams.microsoft.com/l/message/19:abc/msg-1"
    assert doc.metadata == {
        "channel_id": "19:channel-1",
        "author": "Jane Doe",
        "created": "2026-07-15T10:00:00Z",
    }


def test_normalize_reply_message_includes_reply_to_id() -> None:
    connector = TeamsConnector()
    raw_item = _message("msg-2")
    raw_item["_channel_id"] = "19:channel-1"
    raw_item["replyToId"] = "msg-1"

    doc = connector.normalize(raw_item)

    assert doc.metadata["reply_to_id"] == "msg-1"


def test_normalize_message_without_author_omits_author_key() -> None:
    connector = TeamsConnector()
    raw_item = _message(author=None)
    raw_item["_channel_id"] = "19:channel-1"

    doc = connector.normalize(raw_item)

    assert "author" not in doc.metadata


# --- fetch_batch ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_batch_single_page_exhausts_channel() -> None:
    connector = TeamsConnector()
    payload = {"value": [_message("msg-1"), _message("msg-2")]}
    client = _client(
        ["19:channel-1"], **{"teams/team-1/channels/19:channel-1/messages": payload}
    )

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert [item["id"] for item in result.items] == ["msg-1", "msg-2"]
    assert all(item["_channel_id"] == "19:channel-1" for item in result.items)
    assert result.has_more is False
    assert result.next_cursor is None


@pytest.mark.asyncio
async def test_fetch_batch_follows_next_link_within_channel() -> None:
    connector = TeamsConnector()
    next_link = "https://graph.microsoft.com/v1.0/teams/team-1/channels/19:channel-1/messages?$skiptoken=abc"
    payload = {"value": [_message("msg-1")], "@odata.nextLink": next_link}
    client = _client(
        ["19:channel-1"], **{"teams/team-1/channels/19:channel-1/messages": payload}
    )

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.has_more is True
    next_state = json.loads(result.next_cursor)
    assert next_state == {"channel_index": 0, "next_link": next_link}


@pytest.mark.asyncio
async def test_fetch_batch_resumes_from_next_link_cursor() -> None:
    connector = TeamsConnector()
    next_link = "https://graph.microsoft.com/v1.0/teams/team-1/channels/19:channel-1/messages?$skiptoken=abc"
    payload = {"value": [_message("msg-2")]}
    client = _client(["19:channel-1"], **{next_link: payload})
    cursor = json.dumps({"channel_index": 0, "next_link": next_link})

    result = await connector.fetch_batch(client, since=None, cursor=cursor)

    assert [item["id"] for item in result.items] == ["msg-2"]
    assert result.has_more is False


@pytest.mark.asyncio
async def test_fetch_batch_channel_exhausted_advances_to_next_channel() -> None:
    connector = TeamsConnector()
    payload = {"value": [_message("msg-1")]}
    client = _client(
        ["19:channel-1", "19:channel-2"],
        **{"teams/team-1/channels/19:channel-1/messages": payload},
    )

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.has_more is True
    next_state = json.loads(result.next_cursor)
    assert next_state == {"channel_index": 1, "next_link": None}


@pytest.mark.asyncio
async def test_fetch_batch_no_more_channels_returns_empty() -> None:
    connector = TeamsConnector()
    client = _TeamsClient(http=_FakeHttpClient({}), team_id="team-1", channels=[])

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.items == []
    assert result.has_more is False
    assert result.next_cursor is None


@pytest.mark.asyncio
async def test_fetch_batch_full_sync_ignores_since_and_uses_plain_listing() -> None:
    connector = TeamsConnector()
    old = _message("msg-old", created="2026-01-01T00:00:00Z")
    next_link = "https://graph.microsoft.com/v1.0/teams/team-1/channels/19:channel-1/messages?$skiptoken=abc"
    payload = {"value": [old], "@odata.nextLink": next_link}
    client = _client(
        ["19:channel-1"], **{"teams/team-1/channels/19:channel-1/messages": payload}
    )

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert [item["id"] for item in result.items] == ["msg-old"]
    assert result.has_more is True
    next_state = json.loads(result.next_cursor)
    assert next_state == {"channel_index": 0, "next_link": next_link}


# --- fetch_batch: incremental sync (delta query) --------------------------


@pytest.mark.asyncio
async def test_fetch_batch_incremental_starts_fresh_delta_with_filter() -> None:
    connector = TeamsConnector()
    captured: dict[str, Any] = {}

    def fake_delta(params: dict[str, Any]) -> dict[str, Any]:
        captured.update(params)
        return {"value": [_message("msg-1")], "@odata.deltaLink": "https://graph.microsoft.com/delta-token-1"}

    client = _client(
        ["19:channel-1"],
        **{"teams/team-1/channels/19:channel-1/messages/delta": fake_delta},
    )
    since = datetime(2026, 7, 15, tzinfo=timezone.utc)

    result = await connector.fetch_batch(client, since=since, cursor=None)

    assert captured["$filter"] == "lastModifiedDateTime gt 2026-07-15T00:00:00Z"
    assert [item["id"] for item in result.items] == ["msg-1"]
    assert result.has_more is False


@pytest.mark.asyncio
async def test_fetch_batch_incremental_resumes_from_saved_delta_link_without_filter() -> None:
    connector = TeamsConnector()
    saved_delta_link = "https://graph.microsoft.com/delta-token-old"
    payload = {"value": [_message("msg-2")], "@odata.deltaLink": "https://graph.microsoft.com/delta-token-new"}
    client = _client(["19:channel-1"], **{saved_delta_link: payload})
    resume_token = json.dumps({"19:channel-1": saved_delta_link})
    since = datetime(2026, 7, 15, tzinfo=timezone.utc)

    result = await connector.fetch_batch(client, since=since, cursor=None, resume_token=resume_token)

    # Resuming from a saved deltaLink means calling that URL directly --
    # no separate `messages/delta` call with a `$filter` should happen.
    assert client.http.requests[0] == (saved_delta_link, {})
    assert [item["id"] for item in result.items] == ["msg-2"]


@pytest.mark.asyncio
async def test_fetch_batch_incremental_captures_new_delta_link_as_resume_token() -> None:
    connector = TeamsConnector()
    new_delta_link = "https://graph.microsoft.com/delta-token-new"
    payload = {"value": [_message("msg-1")], "@odata.deltaLink": new_delta_link}
    client = _client(
        ["19:channel-1"],
        **{"teams/team-1/channels/19:channel-1/messages/delta": payload},
    )
    since = datetime(2026, 7, 15, tzinfo=timezone.utc)

    result = await connector.fetch_batch(client, since=since, cursor=None)

    assert result.resume_token is not None
    assert json.loads(result.resume_token) == {"19:channel-1": new_delta_link}


@pytest.mark.asyncio
async def test_fetch_batch_incremental_follows_delta_next_link_mid_walk() -> None:
    connector = TeamsConnector()
    next_link = "https://graph.microsoft.com/v1.0/teams/team-1/channels/19:channel-1/messages/delta?$skiptoken=abc"
    payload = {"value": [_message("msg-1")], "@odata.nextLink": next_link}
    client = _client(
        ["19:channel-1"],
        **{"teams/team-1/channels/19:channel-1/messages/delta": payload},
    )
    since = datetime(2026, 7, 15, tzinfo=timezone.utc)

    result = await connector.fetch_batch(client, since=since, cursor=None)

    assert result.has_more is True
    next_state = json.loads(result.next_cursor)
    assert next_state == {"channel_index": 0, "next_link": next_link}
    # Still mid-walk (Graph gave `@odata.nextLink`, not `@odata.deltaLink`
    # yet) -- nothing to persist as a resume token yet.
    assert result.resume_token is None


@pytest.mark.asyncio
async def test_fetch_batch_incremental_resume_state_survives_across_channels() -> None:
    connector = TeamsConnector()
    delta_link_1 = "https://graph.microsoft.com/delta-token-1"
    payload_1 = {"value": [_message("msg-1")], "@odata.deltaLink": delta_link_1}
    delta_link_2 = "https://graph.microsoft.com/delta-token-2"
    payload_2 = {"value": [_message("msg-2")], "@odata.deltaLink": delta_link_2}
    client = _client(
        ["19:channel-1", "19:channel-2"],
        **{
            "teams/team-1/channels/19:channel-1/messages/delta": payload_1,
            "teams/team-1/channels/19:channel-2/messages/delta": payload_2,
        },
    )
    since = datetime(2026, 7, 15, tzinfo=timezone.utc)

    first = await connector.fetch_batch(client, since=since, cursor=None)
    next_state = json.loads(first.next_cursor)
    assert next_state == {"channel_index": 1, "next_link": None}

    second = await connector.fetch_batch(client, since=since, cursor=first.next_cursor)

    # Channel 1's entry (learned on the first call) must still be present
    # even though this call only just learned channel 2's.
    assert json.loads(second.resume_token) == {
        "19:channel-1": delta_link_1,
        "19:channel-2": delta_link_2,
    }


def test_decode_cursor_defaults_to_first_channel() -> None:
    assert TeamsConnector._decode_cursor(None) == (0, None)


def test_decode_cursor_parses_envelope() -> None:
    cursor = json.dumps({"channel_index": 1, "next_link": "https://example.com/next"})
    assert TeamsConnector._decode_cursor(cursor) == (1, "https://example.com/next")
