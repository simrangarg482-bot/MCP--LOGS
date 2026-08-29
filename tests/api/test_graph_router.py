"""Tests for `app.api.routers.graph`.

Same `TestClient` + `dependency_overrides` + stubbed-service style as
`tests/api/test_memory_router.py`. Transport layer only -- authorization and
traversal logic itself is covered in `tests/core/graph/`.

The structural-authorization tests matter most: no route may accept an
`organization_id`, and `depth` can never widen past the hard ceiling.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api import main as api_main
from app.api.deps import get_current_identity
from app.api.routers import graph as graph_router
from app.core.graph.schemas import (
    MAX_TRAVERSAL_DEPTH,
    EntityRef,
    GraphNeighborhood,
    GraphRelationship,
)
from app.database.session import get_db_session
from app.shared.schemas import ActorKind, Identity


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


def _entity(entity_type: str, entity_id: uuid.UUID, label: str | None = None) -> EntityRef:
    return EntityRef(entity_type=entity_type, entity_id=entity_id, label=label)


def _relationship(source: EntityRef, target: EntityRef) -> GraphRelationship:
    return GraphRelationship(
        source=source,
        relationship_type="belongs_to",
        target=target,
        provenance_type="foreign_key",
        meaning="test",
    )


def test_get_direct_relationships_uses_the_callers_identity(client, monkeypatch):
    test_client, actor = client
    entity_id = uuid.uuid4()
    captured: dict = {}

    async def fake_direct(session, passed_actor, entity_type, passed_entity_id):
        captured["actor"] = passed_actor
        captured["entity_type"] = entity_type
        captured["entity_id"] = passed_entity_id
        target = _entity("project", uuid.uuid4())
        return [_relationship(_entity("incident", passed_entity_id), target)]

    monkeypatch.setattr(graph_router.graph_service, "get_direct_relationships", fake_direct)

    response = test_client.get(f"/knowledge-graph/entities/incident/{entity_id}/relationships")

    assert response.status_code == 200
    assert captured["actor"] is actor
    assert captured["entity_type"] == "incident"
    assert captured["entity_id"] == entity_id
    assert len(response.json()) == 1


def test_get_related_entities_passes_depth_through(client, monkeypatch):
    test_client, actor = client
    entity_id = uuid.uuid4()
    captured: dict = {}

    async def fake_neighborhood(session, passed_actor, entity_type, passed_entity_id, *, max_depth):
        captured["max_depth"] = max_depth
        origin = _entity(entity_type, passed_entity_id)
        return GraphNeighborhood(origin=origin, relationships=[], nodes=[origin])

    monkeypatch.setattr(graph_router.graph_service, "get_neighborhood", fake_neighborhood)

    response = test_client.get(f"/knowledge-graph/entities/incident/{entity_id}/related?depth=1")
    assert response.status_code == 200
    assert captured["max_depth"] == 1


def test_get_related_entities_rejects_a_depth_above_the_ceiling(client, monkeypatch):
    """`depth` cannot widen past `MAX_TRAVERSAL_DEPTH` -- FastAPI/Pydantic
    reject an out-of-range value before the service is ever called."""
    test_client, _actor = client
    entity_id = uuid.uuid4()

    async def fake_neighborhood(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("service must not be called with an out-of-range depth")

    monkeypatch.setattr(graph_router.graph_service, "get_neighborhood", fake_neighborhood)

    response = test_client.get(
        f"/knowledge-graph/entities/incident/{entity_id}/related?depth={MAX_TRAVERSAL_DEPTH + 1}"
    )
    assert response.status_code == 422


def test_get_related_entities_defaults_depth_to_the_ceiling(client, monkeypatch):
    test_client, _actor = client
    entity_id = uuid.uuid4()
    captured: dict = {}

    async def fake_neighborhood(session, passed_actor, entity_type, passed_entity_id, *, max_depth):
        captured["max_depth"] = max_depth
        origin = _entity(entity_type, passed_entity_id)
        return GraphNeighborhood(origin=origin, relationships=[], nodes=[origin])

    monkeypatch.setattr(graph_router.graph_service, "get_neighborhood", fake_neighborhood)

    response = test_client.get(f"/knowledge-graph/entities/incident/{entity_id}/related")
    assert response.status_code == 200
    assert captured["max_depth"] == MAX_TRAVERSAL_DEPTH


def test_create_manual_relationship_uses_the_callers_identity(client, monkeypatch):
    test_client, actor = client
    incident_a, incident_b = uuid.uuid4(), uuid.uuid4()
    captured: dict = {}

    async def fake_create(session, passed_actor, data):
        captured["actor"] = passed_actor
        captured["data"] = data
        return _relationship(_entity("incident", incident_a), _entity("incident", incident_b))

    monkeypatch.setattr(graph_router.graph_service, "create_manual_relationship", fake_create)

    response = test_client.post(
        "/knowledge-graph/relationships",
        json={
            "source_entity_type": "incident",
            "source_entity_id": str(incident_a),
            "relationship_type": "related_to",
            "target_entity_type": "incident",
            "target_entity_id": str(incident_b),
        },
    )

    assert response.status_code == 201
    assert captured["actor"] is actor
    assert captured["data"].source_entity_id == incident_a


def test_no_route_accepts_an_organization_id_path_parameter():
    """Guards the API shape itself, the same structural check
    `test_memory_router.py` runs for `/memories`."""
    paths = api_main.app.openapi()["paths"]
    graph_paths = [path for path in paths if path.startswith("/knowledge-graph")]

    assert graph_paths, "knowledge-graph routes should be registered"
    for path in graph_paths:
        assert "{organization_id}" not in path


def test_graph_exposes_exactly_the_intended_operations():
    """Locks the surface down: no `/knowledge-graph/query`, no arbitrary
    expression endpoint -- a future addition has to be deliberate."""
    paths = api_main.app.openapi()["paths"]
    operations = {
        (path, method.upper())
        for path, methods in paths.items()
        if path.startswith("/knowledge-graph")
        for method in methods
    }
    assert operations == {
        ("/knowledge-graph/entities/{entity_type}/{entity_id}/relationships", "GET"),
        ("/knowledge-graph/entities/{entity_type}/{entity_id}/related", "GET"),
        ("/knowledge-graph/relationships", "POST"),
    }
