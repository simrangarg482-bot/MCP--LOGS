"""Tests for `app.api.routers.insights`.

Same `TestClient` + `dependency_overrides` + stubbed-service style as
`tests/api/test_memory_router.py`/`test_graph_router.py`. Transport layer
only -- authorization and detection logic itself is covered in
`tests/core/proactive/`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api import main as api_main
from app.api.deps import get_current_identity
from app.api.routers import insights as insights_router
from app.core.proactive.schemas import FindingDetail, ProactiveFinding
from app.database.session import get_db_session
from app.shared.schemas import ActorKind, Identity

_NOW = datetime(2026, 8, 1, tzinfo=UTC)


@pytest.fixture()
def client():
    organization_id = uuid.uuid4()
    actor = Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
    )

    async def _fake_session():
        yield None

    api_main.app.dependency_overrides[get_current_identity] = lambda: actor
    api_main.app.dependency_overrides[get_db_session] = _fake_session
    yield TestClient(api_main.app), actor
    api_main.app.dependency_overrides.clear()


def _finding(**overrides) -> ProactiveFinding:
    defaults = dict(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        finding_type="recurring_incident_severity",
        status="active",
        title="3 high/critical incidents in the last 14 days",
        summary="s",
        fingerprint="recurring_incident_severity:p1",
        support_count=3,
        detector_name="agent:pattern_detection_agent",
        first_seen_at=_NOW,
        last_seen_at=_NOW,
        deactivated_at=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    defaults.update(overrides)
    return ProactiveFinding(**defaults)


def test_list_findings_uses_the_callers_identity(client, monkeypatch):
    test_client, actor = client
    captured: dict = {}

    async def fake_list(session, passed_actor, *, status, finding_type, limit, offset):
        captured["actor"] = passed_actor
        return [_finding()]

    monkeypatch.setattr(insights_router.proactive_service, "list_findings", fake_list)

    response = test_client.get("/insights")
    assert response.status_code == 200
    assert captured["actor"] is actor
    assert len(response.json()) == 1


def test_list_findings_takes_no_organization_parameter(client, monkeypatch):
    test_client, actor = client
    seen: dict = {}

    async def fake_list(session, passed_actor, *, status, finding_type, limit, offset):
        seen["organization_id"] = passed_actor.organization_id
        return []

    monkeypatch.setattr(insights_router.proactive_service, "list_findings", fake_list)

    other_org = uuid.uuid4()
    response = test_client.get(f"/insights?organization_id={other_org}")

    assert response.status_code == 200
    assert seen["organization_id"] == actor.organization_id
    assert seen["organization_id"] != other_org


def test_list_findings_passes_filters_through(client, monkeypatch):
    test_client, _actor = client
    captured: dict = {}

    async def fake_list(session, actor, *, status, finding_type, limit, offset):
        captured.update(status=status, finding_type=finding_type, limit=limit, offset=offset)
        return []

    monkeypatch.setattr(insights_router.proactive_service, "list_findings", fake_list)

    response = test_client.get(
        "/insights?status=inactive&finding_type=incident_multi_document&limit=5&offset=10"
    )
    assert response.status_code == 200
    assert captured == {
        "status": "inactive",
        "finding_type": "incident_multi_document",
        "limit": 5,
        "offset": 10,
    }


def test_get_finding_passes_the_id_through(client, monkeypatch):
    test_client, _actor = client
    target = uuid.uuid4()
    captured: dict = {}

    async def fake_get(session, actor, finding_id):
        captured["finding_id"] = finding_id
        base = _finding(id=finding_id).model_dump()
        return FindingDetail(**base, supporting_entities=[])

    monkeypatch.setattr(insights_router.proactive_service, "get_finding", fake_get)

    response = test_client.get(f"/insights/{target}")
    assert response.status_code == 200
    assert captured["finding_id"] == target
    assert response.json()["supporting_entities"] == []


def test_no_route_accepts_an_organization_id_path_parameter():
    """Guards the API shape itself, the same structural check
    `test_memory_router.py`/`test_graph_router.py` run for their own
    routers."""
    paths = api_main.app.openapi()["paths"]
    insight_paths = [path for path in paths if path.startswith("/insights")]

    assert insight_paths, "insights routes should be registered"
    for path in insight_paths:
        assert "{organization_id}" not in path


def test_insights_exposes_exactly_the_intended_operations():
    """Locks the surface down: no `POST /insights` (no "detect now"
    endpoint) -- a future addition has to be deliberate."""
    paths = api_main.app.openapi()["paths"]
    operations = {
        (path, method.upper())
        for path, methods in paths.items()
        if path.startswith("/insights")
        for method in methods
    }
    assert operations == {
        ("/insights", "GET"),
        ("/insights/{finding_id}", "GET"),
    }
