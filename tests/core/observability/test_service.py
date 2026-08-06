"""Tests for `app.core.observability.service.get_mcp_dashboard` -- the
Milestone 10 read-path addition. Not a full test suite for `core.
observability.service` (`record_mcp_request` already has no dedicated tests
either, predating this addition). Monkeypatches `repository.
get_mcp_tool_stats`, returning `SimpleNamespace` stand-ins for the raw
SQLAlchemy `Row` objects the real repository function returns (same
attribute-access shape, `.tool_name`/`.request_count`/...).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.core.exceptions import PermissionDeniedError
from app.core.observability import service as observability_service
from app.shared.schemas import ActorKind, Identity


def _reader() -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        permissions=frozenset({"observability:read"}),
    )


@pytest.mark.asyncio
async def test_get_mcp_dashboard_requires_permission() -> None:
    actor = Identity.for_agent("some_agent", uuid.uuid4())
    with pytest.raises(PermissionDeniedError):
        await observability_service.get_mcp_dashboard(None, actor)


@pytest.mark.asyncio
async def test_get_mcp_dashboard_maps_aggregate_rows(monkeypatch) -> None:
    actor = _reader()
    rows = [
        SimpleNamespace(
            tool_name="ask_question",
            request_count=10,
            error_count=2,
            avg_latency_ms=123.4,
            max_latency_ms=500,
        ),
        SimpleNamespace(
            tool_name="investigate_incident",
            request_count=3,
            error_count=0,
            avg_latency_ms=None,
            max_latency_ms=None,
        ),
    ]

    async def fake_get_mcp_tool_stats(session, *, since=None):
        return rows

    monkeypatch.setattr(
        observability_service.repository, "get_mcp_tool_stats", fake_get_mcp_tool_stats
    )

    result = await observability_service.get_mcp_dashboard(None, actor)

    assert len(result) == 2
    assert result[0].tool_name == "ask_question"
    assert result[0].request_count == 10
    assert result[0].error_count == 2
    assert result[0].avg_latency_ms == 123.4
    assert result[1].avg_latency_ms is None
    assert result[1].max_latency_ms is None
