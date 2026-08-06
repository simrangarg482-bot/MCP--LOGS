"""Tests for `app.api.routers.observability` -- same `TestClient` +
`dependency_overrides` + stubbed-service style as
`tests/api/test_knowledge_router.py`.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.agents.schemas import AgentExecutionStats
from app.api import main as api_main
from app.api.deps import get_current_identity
from app.api.routers import observability as observability_router
from app.core.observability.schemas import McpToolStats
from app.database.session import get_db_session
from app.shared.schemas import ActorKind, Identity


def _reader() -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        permissions=frozenset({"observability:read"}),
    )


@pytest.fixture()
def client():
    actor = _reader()

    async def _fake_session():
        yield None

    api_main.app.dependency_overrides[get_current_identity] = lambda: actor
    api_main.app.dependency_overrides[get_db_session] = _fake_session

    yield TestClient(api_main.app), actor

    api_main.app.dependency_overrides.clear()


def test_get_agent_execution_stats_returns_stats(client, monkeypatch) -> None:
    test_client, actor = client
    stats = AgentExecutionStats(
        agent_name="answer_question",
        execution_count=10,
        succeeded_count=9,
        failed_count=1,
        avg_confidence_score=0.8,
        avg_latency_seconds=1.2,
    )

    async def fake_get_agent_execution_stats(session, passed_actor, *, since=None):
        assert passed_actor is actor
        assert since is None
        return [stats]

    monkeypatch.setattr(
        observability_router.agents_service,
        "get_agent_execution_stats",
        fake_get_agent_execution_stats,
    )

    response = test_client.get("/observability/agents")

    assert response.status_code == 200
    assert response.json()[0]["agent_name"] == "answer_question"
    assert response.json()[0]["succeeded_count"] == 9


def test_get_agent_execution_stats_passes_since_query_param(client, monkeypatch) -> None:
    test_client, actor = client
    captured: dict[str, object] = {}

    async def fake_get_agent_execution_stats(session, passed_actor, *, since=None):
        captured["since"] = since
        return []

    monkeypatch.setattr(
        observability_router.agents_service,
        "get_agent_execution_stats",
        fake_get_agent_execution_stats,
    )

    response = test_client.get("/observability/agents", params={"since": "2026-07-01T00:00:00Z"})

    assert response.status_code == 200
    assert captured["since"] is not None


def test_get_mcp_dashboard_returns_stats(client, monkeypatch) -> None:
    test_client, actor = client
    stats = McpToolStats(
        tool_name="ask_question",
        request_count=5,
        error_count=1,
        avg_latency_ms=200.0,
        max_latency_ms=900,
    )

    async def fake_get_mcp_dashboard(session, passed_actor, *, since=None):
        assert passed_actor is actor
        return [stats]

    monkeypatch.setattr(
        observability_router.observability_service, "get_mcp_dashboard", fake_get_mcp_dashboard
    )

    response = test_client.get("/observability/mcp")

    assert response.status_code == 200
    assert response.json()[0]["tool_name"] == "ask_question"
    assert response.json()[0]["error_count"] == 1
