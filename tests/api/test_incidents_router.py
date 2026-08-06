"""End-to-end-through-FastAPI tests for `app.api.routers.incidents`.

Exercises real request routing, dependency injection, and Pydantic
request/response (de)serialization via `TestClient` against the actual
`app.api.main.app` -- only `get_current_identity` (auth) and the
`core.incidents.service` calls themselves are stubbed, via
`app.dependency_overrides` and `monkeypatch` respectively, so no database or
bearer token is needed. `get_db_session` is overridden to yield `None`: safe
here because the stubbed service functions never touch it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.api import main as api_main
from app.api.deps import get_current_identity
from app.api.routers import incidents as incidents_router
from app.core.incidents.schemas import Incident
from app.database.session import get_db_session
from app.shared.schemas import Identity


def _actor() -> Identity:
    return Identity.for_agent("test_actor", uuid.uuid4())


def _incident(actor: Identity, **overrides: object) -> Incident:
    now = datetime.now(timezone.utc)
    defaults: dict[str, object] = dict(
        id=uuid.uuid4(),
        organization_id=actor.organization_id,
        project_id=uuid.uuid4(),
        title="Checkout down",
        description="Checkout returns 500s.",
        status="open",
        severity="high",
        owner_team=None,
        reported_by=uuid.uuid4(),
        resolved_at=None,
        created_at=now,
        updated_at=now,
    )
    defaults.update(overrides)
    return Incident(**defaults)


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    actor = _actor()

    async def _fake_session():
        yield None

    api_main.app.dependency_overrides[get_current_identity] = lambda: actor
    api_main.app.dependency_overrides[get_db_session] = _fake_session

    yield TestClient(api_main.app), actor

    api_main.app.dependency_overrides.clear()


def test_create_incident_returns_201_and_the_created_incident(client, monkeypatch) -> None:
    test_client, actor = client
    created = _incident(actor, title="New incident")

    async def fake_create_incident(session, passed_actor, organization_id, data):
        assert passed_actor is actor
        assert organization_id == actor.organization_id
        assert data.title == "New incident"
        return created

    monkeypatch.setattr(incidents_router.incidents_service, "create_incident", fake_create_incident)

    response = test_client.post(
        "/incidents",
        json={"title": "New incident", "description": "desc", "severity": "high"},
    )

    assert response.status_code == 201
    assert response.json()["id"] == str(created.id)
    assert response.json()["title"] == "New incident"


def test_get_incident_returns_incident(client, monkeypatch) -> None:
    test_client, actor = client
    fetched = _incident(actor)

    async def fake_get_incident(session, passed_actor, organization_id, incident_id):
        assert incident_id == fetched.id
        return fetched

    monkeypatch.setattr(incidents_router.incidents_service, "get_incident", fake_get_incident)

    response = test_client.get(f"/incidents/{fetched.id}")

    assert response.status_code == 200
    assert response.json()["id"] == str(fetched.id)


def test_list_incidents_passes_query_params_as_filter(client, monkeypatch) -> None:
    test_client, actor = client
    captured: dict[str, object] = {}

    async def fake_list_incidents(session, passed_actor, organization_id, query):
        captured["query"] = query
        return [_incident(actor)]

    monkeypatch.setattr(incidents_router.incidents_service, "list_incidents", fake_list_incidents)

    response = test_client.get("/incidents", params={"status": "open", "limit": 5, "offset": 10})

    assert response.status_code == 200
    assert len(response.json()) == 1
    query = captured["query"]
    assert query.status == "open"
    assert query.limit == 5
    assert query.offset == 10


def test_update_incident_returns_updated_incident(client, monkeypatch) -> None:
    test_client, actor = client
    updated = _incident(actor, status="resolved")

    async def fake_update_incident(session, passed_actor, organization_id, incident_id, patch):
        assert patch.status == "resolved"
        return updated

    monkeypatch.setattr(incidents_router.incidents_service, "update_incident", fake_update_incident)

    response = test_client.patch(f"/incidents/{updated.id}", json={"status": "resolved"})

    assert response.status_code == 200
    assert response.json()["status"] == "resolved"


def test_add_timeline_note_returns_201(client, monkeypatch) -> None:
    test_client, actor = client
    incident_id = uuid.uuid4()

    from app.core.incidents.schemas import TimelineEntry

    entry = TimelineEntry(
        id=uuid.uuid4(),
        organization_id=actor.organization_id,
        incident_id=incident_id,
        event_type="note",
        event_data={"note": "checked logs"},
        actor=actor.audit_tag,
        occurred_at=datetime.now(timezone.utc),
    )

    async def fake_add_timeline_note(session, passed_actor, organization_id, passed_incident_id, data):
        assert data.note == "checked logs"
        return entry

    monkeypatch.setattr(
        incidents_router.incidents_service, "add_timeline_note", fake_add_timeline_note
    )

    response = test_client.post(
        f"/incidents/{incident_id}/timeline", json={"note": "checked logs"}
    )

    assert response.status_code == 201
    assert response.json()["event_data"] == {"note": "checked logs"}
