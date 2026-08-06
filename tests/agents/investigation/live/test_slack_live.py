"""Tests for `app.agents.investigation.live.slack_live.SlackLiveSource`."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from app.agents.investigation.live.slack_live import SlackLiveSource
from app.core.tenancy.schemas import ConnectorConfig


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

    async def __aenter__(self) -> "_FakeHttpClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def get(self, url: str, params: dict[str, Any] | None = None) -> _FakeResponse:
        params = params or {}
        value = self._responses[url]
        payload = value(params) if callable(value) else value
        return _FakeResponse(payload)


def _connector_config(channels: list[str]) -> ConnectorConfig:
    now = datetime.now(timezone.utc)
    return ConnectorConfig(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        project_id=None,
        source="slack",
        credential_ref="xoxb-test-token",
        config={"channels": channels},
        status="active",
        last_synced_at=None,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_fetch_live_evidence_returns_empty_without_channels() -> None:
    source = SlackLiveSource()
    connector_config = _connector_config([])

    result = await source.fetch_live_evidence(
        connector_config=connector_config,
        query="checkout failing",
        since=datetime.now(timezone.utc),
        limit=5,
    )

    assert result == []


@pytest.mark.asyncio
async def test_fetch_live_evidence_filters_by_keyword_and_flags_live(monkeypatch) -> None:
    source = SlackLiveSource()
    connector_config = _connector_config(["C0123456"])

    payload = {
        "ok": True,
        "messages": [
            {"ts": "1722600000.000100", "text": "checkout is throwing 500s", "user": "U1"},
            {"ts": "1722600100.000200", "text": "totally unrelated lunch chat", "user": "U2"},
        ],
    }
    fake_http = _FakeHttpClient({"conversations.history": payload})
    monkeypatch.setattr(
        "app.agents.investigation.live.slack_live.httpx.AsyncClient",
        lambda *args, **kwargs: fake_http,
    )

    result = await source.fetch_live_evidence(
        connector_config=connector_config,
        query="checkout 500 error",
        since=datetime(2026, 7, 1, tzinfo=timezone.utc),
        limit=5,
    )

    assert len(result) == 1
    item = result[0]
    assert item.source == "slack"
    assert "checkout" in item.summary
    assert item.metadata["retrieval_mode"] == "live"
    assert item.metadata["channel_id"] == "C0123456"
    assert item.metadata["user"] == "U1"


@pytest.mark.asyncio
async def test_fetch_live_evidence_handles_slack_api_error(monkeypatch) -> None:
    source = SlackLiveSource()
    connector_config = _connector_config(["C0123456"])

    payload = {"ok": False, "error": "invalid_auth"}
    fake_http = _FakeHttpClient({"conversations.history": payload})
    monkeypatch.setattr(
        "app.agents.investigation.live.slack_live.httpx.AsyncClient",
        lambda *args, **kwargs: fake_http,
    )

    result = await source.fetch_live_evidence(
        connector_config=connector_config,
        query="checkout",
        since=datetime(2026, 7, 1, tzinfo=timezone.utc),
        limit=5,
    )

    assert result == []


@pytest.mark.asyncio
async def test_fetch_live_evidence_with_no_keywords_matches_everything(monkeypatch) -> None:
    """A query with only short/common words (below `_MIN_KEYWORD_LENGTH`)
    produces no usable keywords -- falls back to "match everything recent"
    rather than matching nothing.
    """
    source = SlackLiveSource()
    connector_config = _connector_config(["C0123456"])

    payload = {
        "ok": True,
        "messages": [{"ts": "1722600000.000100", "text": "anything at all", "user": "U1"}],
    }
    fake_http = _FakeHttpClient({"conversations.history": payload})
    monkeypatch.setattr(
        "app.agents.investigation.live.slack_live.httpx.AsyncClient",
        lambda *args, **kwargs: fake_http,
    )

    result = await source.fetch_live_evidence(
        connector_config=connector_config,
        query="is to a",  # all below _MIN_KEYWORD_LENGTH
        since=datetime(2026, 7, 1, tzinfo=timezone.utc),
        limit=5,
    )

    assert len(result) == 1
