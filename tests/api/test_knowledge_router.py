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
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.shared.schemas import ActorKind, GapReport, Identity


def _reviewer() -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        permissions=frozenset({"knowledge:review"}),
    )


def _member_no_permissions(organization_id: uuid.UUID | None = None) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id or uuid.uuid4(),
        user_id=uuid.uuid4(),
        permissions=frozenset(),
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


# --- GET /knowledge pagination (Stage 3) ----------------------------------


def test_list_published_returns_documents(client, monkeypatch) -> None:
    test_client, actor = client
    doc = _document(actor, status="published")

    async def fake_list_published_documents(
        session, passed_actor, organization_id, *, source, updated_since, limit, offset
    ):
        assert passed_actor is actor
        assert limit == 25
        assert offset == 50
        return [doc]

    monkeypatch.setattr(
        knowledge_router.knowledge_service, "list_published_documents", fake_list_published_documents
    )

    response = test_client.get("/knowledge?limit=25&offset=50")

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["id"] == str(doc.id)


def test_list_published_rejects_unbounded_or_non_positive_limit(client) -> None:
    test_client, _actor = client
    assert test_client.get("/knowledge?limit=101").status_code == 422
    assert test_client.get("/knowledge?limit=0").status_code == 422


def test_list_published_rejects_negative_offset(client) -> None:
    test_client, _actor = client
    assert test_client.get("/knowledge?offset=-1").status_code == 422


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


def test_dismiss_gap_report_returns_dismissed_report(client, monkeypatch) -> None:
    test_client, actor = client
    report = _gap_report(actor, status="dismissed")

    async def fake_dismiss_gap_report(session, passed_actor, gap_report_id):
        assert passed_actor is actor
        assert gap_report_id == report.id
        return report

    monkeypatch.setattr(
        knowledge_router.agents_service, "dismiss_gap_report", fake_dismiss_gap_report
    )

    response = test_client.post(f"/knowledge/gaps/{report.id}/dismiss")

    assert response.status_code == 200
    assert response.json()["id"] == str(report.id)
    assert response.json()["status"] == "dismissed"


def test_dismiss_gap_report_propagates_permission_denied_as_403(client, monkeypatch) -> None:
    """Router-layer proof that a service-raised `PermissionDeniedError`
    reaches the caller as HTTP 403 -- the actual permission check itself
    (missing `knowledge:review`) is `agents.service.dismiss_gap_report`'s
    own responsibility, covered in `tests/agents/test_service.py`.
    """
    test_client, _actor = client

    async def fake_dismiss_gap_report(session, passed_actor, gap_report_id):
        raise PermissionDeniedError("denied", error_code="permission_denied")

    monkeypatch.setattr(
        knowledge_router.agents_service, "dismiss_gap_report", fake_dismiss_gap_report
    )

    response = test_client.post(f"/knowledge/gaps/{uuid.uuid4()}/dismiss")

    assert response.status_code == 403


def test_dismiss_gap_report_propagates_not_found_as_404(client, monkeypatch) -> None:
    """Router-layer proof that a service-raised `NotFoundError` -- the same
    error `dismiss_gap_report` raises for both a nonexistent id and a
    cross-organization one (see that function's own docstring) -- reaches
    the caller as HTTP 404. The actual organization-scoping itself is
    `repository.update_gap_report_status`'s responsibility, covered in
    `tests/agents/knowledge_gap/test_repository.py` and
    `tests/agents/test_service.py`.
    """
    test_client, _actor = client

    async def fake_dismiss_gap_report(session, passed_actor, gap_report_id):
        raise NotFoundError("not found", error_code="gap_report.not_found")

    monkeypatch.setattr(
        knowledge_router.agents_service, "dismiss_gap_report", fake_dismiss_gap_report
    )

    response = test_client.post(f"/knowledge/gaps/{uuid.uuid4()}/dismiss")

    assert response.status_code == 404


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
    docstring: `/knowledge/proposed`, `/knowledge/gaps`, and
    `/knowledge/gaps/{id}/dismiss` must still resolve to their own
    literal-prefix handlers, not be swallowed by `GET /{document_id}` as if
    `"proposed"`/`"gaps"` were a document id.
    """
    test_client, actor = client

    async def fake_list_proposed_documents(
        session, passed_actor, organization_id, *, limit, offset
    ):
        return []

    async def fake_list_gap_reports(session, passed_actor):
        return []

    async def fake_dismiss_gap_report(session, passed_actor, gap_report_id):
        return _gap_report(actor, id=gap_report_id, status="dismissed")

    async def fake_get_document(session, passed_actor, organization_id, document_id):
        raise AssertionError("get_document must not be called for /proposed, /gaps, or /gaps/*/dismiss")

    monkeypatch.setattr(
        knowledge_router.knowledge_service, "list_proposed_documents", fake_list_proposed_documents
    )
    monkeypatch.setattr(knowledge_router.agents_service, "list_gap_reports", fake_list_gap_reports)
    monkeypatch.setattr(
        knowledge_router.agents_service, "dismiss_gap_report", fake_dismiss_gap_report
    )
    monkeypatch.setattr(knowledge_router.knowledge_service, "get_document", fake_get_document)

    assert test_client.get("/knowledge/proposed").status_code == 200
    assert test_client.get("/knowledge/gaps").status_code == 200
    assert test_client.post(f"/knowledge/gaps/{uuid.uuid4()}/dismiss").status_code == 200


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


# --- POST /knowledge (P3: human REST path for proposing a document) --------


def test_propose_document_creates_proposed_document(client, monkeypatch) -> None:
    test_client, actor = client
    created = _document(actor, title="Restart the payment worker", content="Steps: ...")
    captured: dict[str, object] = {}

    async def fake_propose_document(session, passed_actor, organization_id, data):
        captured["actor"] = passed_actor
        captured["organization_id"] = organization_id
        captured["data"] = data
        return created

    monkeypatch.setattr(knowledge_router.knowledge_service, "propose_document", fake_propose_document)

    response = test_client.post(
        "/knowledge", json={"title": "Restart the payment worker", "content": "Steps: ..."}
    )

    assert response.status_code == 201
    assert response.json()["status"] == "proposed"
    assert captured["actor"] is actor
    assert captured["organization_id"] == actor.organization_id
    assert captured["data"].title == "Restart the payment worker"
    assert captured["data"].content == "Steps: ..."


def test_propose_document_does_not_require_knowledge_review_permission(monkeypatch) -> None:
    """`propose_document` itself has no permission gate (any authenticated
    org member may submit a proposal; only publish/reject require
    `knowledge:review`) -- this must hold through the REST path too, not
    just the MCP tool.
    """
    actor = _member_no_permissions()
    created = _document(actor)

    async def _fake_session():
        yield None

    async def fake_propose_document(session, passed_actor, organization_id, data):
        return created

    api_main.app.dependency_overrides[get_current_identity] = lambda: actor
    api_main.app.dependency_overrides[get_db_session] = _fake_session
    monkeypatch.setattr(knowledge_router.knowledge_service, "propose_document", fake_propose_document)

    try:
        response = TestClient(api_main.app).post(
            "/knowledge", json={"title": "A runbook", "content": "do the thing"}
        )
    finally:
        api_main.app.dependency_overrides.clear()

    assert response.status_code == 201


def test_propose_document_rejects_invalid_body(client) -> None:
    """Router-level validation (missing required `content`) -- the same
    Pydantic/FastAPI request-validation behavior every other endpoint in
    this app already gets, reused here rather than reimplemented.
    """
    test_client, _actor = client

    response = test_client.post("/knowledge", json={"title": "Missing content"})

    assert response.status_code == 422


def test_propose_document_uses_callers_own_organization_not_a_client_supplied_one(
    client, monkeypatch
) -> None:
    """`DocumentProposalCreate` has no `organization_id` field at all -- a
    proposal is always scoped to `actor.organization_id`, never something
    the client could supply, which is what actually preserves organization
    scoping at this layer (the cross-organization rejection itself is
    `propose_document`'s own job, covered by
    `test_propose_document_denies_cross_organization` in
    `tests/core/knowledge/test_service.py`, unchanged by this endpoint).
    """
    test_client, actor = client
    created = _document(actor)
    captured: dict[str, object] = {}

    async def fake_propose_document(session, passed_actor, organization_id, data):
        captured["organization_id"] = organization_id
        return created

    monkeypatch.setattr(knowledge_router.knowledge_service, "propose_document", fake_propose_document)

    test_client.post("/knowledge", json={"title": "t", "content": "c"})

    assert captured["organization_id"] == actor.organization_id
