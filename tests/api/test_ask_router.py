"""Tests for `app.api.routers.ask` -- `POST /ask` and
`POST /incidents/{id}/investigate`, via `TestClient` against the real
`app.api.main.app` with `get_current_identity`/`get_db_session` overridden
and `agents.service` calls stubbed (same pattern as
`tests/api/test_incidents_router.py`).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.agents import service as agents_service
from app.api import main as api_main
from app.api.deps import get_current_identity
from app.api.routers import ask as ask_router
from app.database.session import get_db_session
from app.shared.schemas import AskResponse, Identity


def _actor() -> Identity:
    return Identity.for_agent("test_actor", uuid.uuid4())


@pytest.fixture()
def client():
    actor = _actor()

    async def _fake_session():
        yield None

    api_main.app.dependency_overrides[get_current_identity] = lambda: actor
    api_main.app.dependency_overrides[get_db_session] = _fake_session

    yield TestClient(api_main.app), actor

    api_main.app.dependency_overrides.clear()


def test_ask_question_calls_answer_question_with_core_api_trigger_source(client, monkeypatch) -> None:
    test_client, actor = client
    captured: dict[str, object] = {}
    response_payload = AskResponse(confidence=0.9, route_taken="answer", answer="It's X.", citations=[])

    async def fake_answer_question(session, query, incident_id, passed_actor, *, trigger_source="core_api"):
        captured["query"] = query
        captured["incident_id"] = incident_id
        captured["trigger_source"] = trigger_source
        assert passed_actor is actor
        return response_payload

    monkeypatch.setattr(ask_router.agents_service, "answer_question", fake_answer_question)

    response = test_client.post("/ask", json={"query": "why is checkout failing?"})

    assert response.status_code == 200
    assert response.json()["answer"] == "It's X."
    assert captured["query"] == "why is checkout failing?"
    assert captured["incident_id"] is None
    # REST leaves trigger_source at agents_service.answer_question's own
    # default ("core_api") -- confirms the router does not pass "mcp".
    assert captured["trigger_source"] == "core_api"


def test_investigate_incident_calls_triage_incident(client, monkeypatch) -> None:
    test_client, actor = client
    incident_id = uuid.uuid4()
    response_payload = AskResponse(
        confidence=0.4,
        route_taken="investigation",
        investigation=None,
    )

    async def fake_triage_incident(session, passed_incident_id, passed_actor, *, trigger_source="core_api"):
        assert passed_incident_id == incident_id
        assert passed_actor is actor
        return response_payload

    monkeypatch.setattr(ask_router.agents_service, "triage_incident", fake_triage_incident)

    response = test_client.post(f"/incidents/{incident_id}/investigate")

    assert response.status_code == 200
    assert response.json()["route_taken"] == "investigation"
