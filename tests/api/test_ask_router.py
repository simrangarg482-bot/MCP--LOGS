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


def test_ask_question_calls_answer_question_with_core_api_trigger_source(
    client, monkeypatch
) -> None:
    test_client, actor = client
    captured: dict[str, object] = {}
    response_payload = AskResponse(
        confidence=0.9, route_taken="answer", answer="It's X.", citations=[]
    )

    async def fake_answer_question(
        session, query, incident_id, passed_actor, *, trigger_source="core_api"
    ):
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

    async def fake_triage_incident(
        session, passed_incident_id, passed_actor, *, trigger_source="core_api"
    ):
        assert passed_incident_id == incident_id
        assert passed_actor is actor
        return response_payload

    monkeypatch.setattr(ask_router.agents_service, "triage_incident", fake_triage_incident)

    response = test_client.post(f"/incidents/{incident_id}/investigate")

    assert response.status_code == 200
    assert response.json()["route_taken"] == "investigation"


# --------------------------------------------------------------------------
# `answer_mode` (Priority 10) -- serialization + backward compatibility
# --------------------------------------------------------------------------


def test_ask_response_serializes_answer_mode_when_answered(client, monkeypatch) -> None:
    test_client, actor = client
    response_payload = AskResponse(
        confidence=0.9, route_taken="answer", answer="It's X.", answer_mode="answered", citations=[]
    )

    async def fake_answer_question(
        session, query, incident_id, passed_actor, *, trigger_source="core_api"
    ):
        return response_payload

    monkeypatch.setattr(ask_router.agents_service, "answer_question", fake_answer_question)

    response = test_client.post("/ask", json={"query": "why is checkout failing?"})

    assert response.status_code == 200
    assert response.json()["answer_mode"] == "answered"


def test_ask_response_serializes_answer_mode_when_no_answer(client, monkeypatch) -> None:
    test_client, actor = client
    response_payload = AskResponse(
        confidence=0.2,
        route_taken="answer",
        answer="I don't have enough grounded information...",
        answer_mode="no_answer",
        citations=[],
    )

    async def fake_answer_question(
        session, query, incident_id, passed_actor, *, trigger_source="core_api"
    ):
        return response_payload

    monkeypatch.setattr(ask_router.agents_service, "answer_question", fake_answer_question)

    response = test_client.post("/ask", json={"query": "what's our AWS spend?"})

    assert response.status_code == 200
    assert response.json()["answer_mode"] == "no_answer"


def test_ask_response_answer_mode_defaults_to_null_not_answered(client, monkeypatch) -> None:
    """Backward compatibility: a response constructed without `answer_mode`
    (e.g. by code that hasn't been updated yet, or in the investigation
    route where no answer-path decision was made) must serialize `null`,
    never silently claim `"answered"`."""
    test_client, actor = client
    response_payload = AskResponse(
        confidence=0.9, route_taken="answer", answer="It's X.", citations=[]
    )

    async def fake_answer_question(
        session, query, incident_id, passed_actor, *, trigger_source="core_api"
    ):
        return response_payload

    monkeypatch.setattr(ask_router.agents_service, "answer_question", fake_answer_question)

    response = test_client.post("/ask", json={"query": "why is checkout failing?"})

    assert response.status_code == 200
    assert response.json()["answer_mode"] is None


def test_ask_request_still_accepts_only_query_and_incident_id(client, monkeypatch) -> None:
    """No new REQUIRED request parameter was introduced -- the existing
    minimal `{"query": ...}` payload must still be accepted."""
    test_client, actor = client
    response_payload = AskResponse(confidence=0.9, route_taken="answer", answer="ok", citations=[])

    async def fake_answer_question(
        session, query, incident_id, passed_actor, *, trigger_source="core_api"
    ):
        return response_payload

    monkeypatch.setattr(ask_router.agents_service, "answer_question", fake_answer_question)

    response = test_client.post("/ask", json={"query": "a minimal request"})
    assert response.status_code == 200


def test_openapi_schema_declares_answer_mode_as_a_two_value_optional_enum() -> None:
    """The OpenAPI contract itself must reflect the new field: nullable,
    and only the two production-supported values -- never a third
    `qualified_answer` value production cannot actually produce."""
    schema = api_main.app.openapi()
    prop = schema["components"]["schemas"]["AskResponse"]["properties"]["answer_mode"]

    variants = prop["anyOf"]
    enum_variant = next(v for v in variants if v.get("type") == "string")
    null_variant = next(v for v in variants if v.get("type") == "null")

    assert set(enum_variant["enum"]) == {"answered", "no_answer"}
    assert null_variant is not None  # nullable -- unknown/not-applicable is representable

    required = schema["components"]["schemas"]["AskResponse"].get("required", [])
    assert "answer_mode" not in required  # additive, never a required response field
