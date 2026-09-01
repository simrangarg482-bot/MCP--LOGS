"""Tests for `app.api.routers.knowledge` -- same `TestClient` +
`dependency_overrides` + stubbed-service style as
`tests/api/test_incidents_router.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import main as api_main
from app.api.deps import get_current_identity
from app.api.routers import knowledge as knowledge_router
from app.core.knowledge.schemas import Document
from app.database.session import get_db_session
from app.shared.schemas import ActorKind, GapReport, Identity


def _reviewer() -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        permissions=frozenset({"knowledge:review"}),
    )


def _document(actor: Identity, **overrides: object) -> Document:
    now = datetime.now(timezone.utc)
    defaults: dict[str, object] = dict(
        id=uuid.uuid4(),
        organization_id=actor.organization_id,
        project_id=uuid.uuid4(),
        title="A runbook",
        status="proposed",
        version=1,
        content="do the thing",
        source="manual",
        source_incident_id=None,
        created_at=now,
        updated_at=now,
    )
    defaults.update(overrides)
    return Document(**defaults)


@pytest.fixture()
def client():
    actor = _reviewer()

    async def _fake_session():
        yield None

    api_main.app.dependency_overrides[get_current_identity] = lambda: actor
    api_main.app.dependency_overrides[get_db_session] = _fake_session

    yield TestClient(api_main.app), actor

    api_main.app.dependency_overrides.clear()


def test_list_proposed_returns_documents(client, monkeypatch) -> None:
    test_client, actor = client
    doc = _document(actor)

    async def fake_list_proposed_documents(
        session, passed_actor, organization_id, *, limit, offset
    ):
        assert passed_actor is actor
        assert limit == 25
        assert offset == 50
        return [doc]

    monkeypatch.setattr(
        knowledge_router.knowledge_service, "list_proposed_documents", fake_list_proposed_documents
    )

    response = test_client.get("/knowledge/proposed?limit=25&offset=50")

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["id"] == str(doc.id)


def test_list_proposed_rejects_unbounded_page(client) -> None:
    test_client, _actor = client
    assert test_client.get("/knowledge/proposed?limit=101").status_code == 422


def _gap_report(actor: Identity, **overrides: object) -> GapReport:
    now = datetime.now(timezone.utc)
    defaults: dict[str, object] = dict(
        id=uuid.uuid4(),
        organization_id=actor.organization_id,
        suggested_topic="Checkout reliability",
        supporting_execution_ids=[uuid.uuid4(), uuid.uuid4()],
        suggested_action="new_runbook",
        related_document_id=None,
        status="open",
        created_at=now,
        updated_at=now,
    )
    defaults.update(overrides)
    return GapReport(**defaults)


def test_list_gap_reports_returns_reports(client, monkeypatch) -> None:
    test_client, actor = client
    report = _gap_report(actor)

    async def fake_list_gap_reports(session, passed_actor):
        assert passed_actor is actor
        return [report]

    monkeypatch.setattr(knowledge_router.agents_service, "list_gap_reports", fake_list_gap_reports)

    response = test_client.get("/knowledge/gaps")

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["id"] == str(report.id)
    assert response.json()[0]["suggested_action"] == "new_runbook"


def test_publish_returns_published_document(client, monkeypatch) -> None:
    test_client, actor = client
    published = _document(actor, status="published")

    async def fake_publish_document(session, passed_actor, organization_id, document_id):
        assert document_id == published.id
        return published

    monkeypatch.setattr(knowledge_router.knowledge_service, "publish_document", fake_publish_document)

    response = test_client.post(f"/knowledge/{published.id}/publish")

    assert response.status_code == 200
    assert response.json()["status"] == "published"


def test_reject_returns_document(client, monkeypatch) -> None:
    test_client, actor = client
    doc = _document(actor)

    async def fake_reject_document(session, passed_actor, organization_id, document_id):
        assert document_id == doc.id
        return doc

    monkeypatch.setattr(knowledge_router.knowledge_service, "reject_document", fake_reject_document)

    response = test_client.post(f"/knowledge/{doc.id}/reject")

    assert response.status_code == 200
    assert response.json()["id"] == str(doc.id)


def test_get_document_returns_document(client, monkeypatch) -> None:
    test_client, actor = client
    doc = _document(actor)

    async def fake_get_document(session, passed_actor, organization_id, document_id):
        assert passed_actor is actor
        assert document_id == doc.id
        return doc

    monkeypatch.setattr(knowledge_router.knowledge_service, "get_document", fake_get_document)

    response = test_client.get(f"/knowledge/{doc.id}")

    assert response.status_code == 200
    assert response.json()["id"] == str(doc.id)


def test_get_document_route_does_not_shadow_proposed_or_gaps(client, monkeypatch) -> None:
    """Regression test for the route-ordering note in the router's own
    docstring: `/knowledge/proposed` and `/knowledge/gaps` must still resolve
    to their own literal-path handlers, not be swallowed by
    `GET /{document_id}` as if `"proposed"`/`"gaps"` were a document id.
    """
    test_client, actor = client

    async def fake_list_proposed_documents(
        session, passed_actor, organization_id, *, limit, offset
    ):
        return []

    async def fake_list_gap_reports(session, passed_actor):
        return []

    async def fake_get_document(session, passed_actor, organization_id, document_id):
        raise AssertionError("get_document must not be called for /proposed or /gaps")

    monkeypatch.setattr(
        knowledge_router.knowledge_service, "list_proposed_documents", fake_list_proposed_documents
    )
    monkeypatch.setattr(knowledge_router.agents_service, "list_gap_reports", fake_list_gap_reports)
    monkeypatch.setattr(knowledge_router.knowledge_service, "get_document", fake_get_document)

    assert test_client.get("/knowledge/proposed").status_code == 200
    assert test_client.get("/knowledge/gaps").status_code == 200


def test_update_document_returns_updated_document(client, monkeypatch) -> None:
    test_client, actor = client
    doc = _document(actor)
    updated = _document(actor, id=doc.id, title="New title", version=2)
    captured: dict[str, object] = {}

    async def fake_update_document(session, passed_actor, organization_id, document_id, data):
        captured["document_id"] = document_id
        captured["data"] = data
        return updated

    monkeypatch.setattr(knowledge_router.knowledge_service, "update_document", fake_update_document)

    response = test_client.patch(f"/knowledge/{doc.id}", json={"title": "New title"})

    assert response.status_code == 200
    assert response.json()["title"] == "New title"
    assert response.json()["version"] == 2
    assert captured["document_id"] == doc.id
    assert captured["data"].title == "New title"
    assert captured["data"].content is None  # exclude_unset: omitted field stays None on the model
