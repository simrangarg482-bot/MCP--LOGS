"""Tests for `app.api.routers.memory`.

Same `TestClient` + `dependency_overrides` + stubbed-service style as
`tests/api/test_tenancy_router.py`. Transport layer only -- the ownership and
visibility logic itself is covered in `tests/core/memory/`.

The structural-authorization tests matter most: the API must offer no
parameter through which a caller could target another organization or
another user's memory.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api import main as api_main
from app.api.deps import get_current_identity
from app.api.routers import memory as memory_router
from app.core.memory.schemas import Memory
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


def _memory(actor: Identity, **overrides) -> Memory:
    defaults = dict(
        id=uuid.uuid4(),
        organization_id=actor.organization_id,
        scope="user",
        owner_user_id=actor.user_id,
        project_id=None,
        memory_type="preference",
        content="Primary region is europe",
        source_type="explicit",
        source_id=None,
        created_by=actor.audit_tag,
        status="active",
        supersedes_memory_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    defaults.update(overrides)
    return Memory(**defaults)


def test_create_memory_uses_the_callers_identity(client, monkeypatch):
    test_client, actor = client
    captured: dict = {}

    async def fake_create(session, passed_actor, data):
        captured["actor"] = passed_actor
        captured["data"] = data
        return _memory(actor)

    monkeypatch.setattr(memory_router.memory_service, "create_memory", fake_create)

    response = test_client.post(
        "/memories",
        json={"scope": "user", "memory_type": "preference", "content": "Primary region is europe"},
    )

    assert response.status_code == 201
    assert captured["actor"] is actor
    assert captured["data"].scope == "user"


def test_create_memory_rejects_an_owner_user_id_in_the_body(client, monkeypatch):
    """Structural authorization: `MemoryCreate` has no `owner_user_id` field,
    so a caller cannot create memory owned by someone else. Pydantic ignores
    the unknown key rather than honoring it."""
    test_client, actor = client
    captured: dict = {}

    async def fake_create(session, passed_actor, data):
        captured["data"] = data
        return _memory(actor)

    monkeypatch.setattr(memory_router.memory_service, "create_memory", fake_create)

    victim = uuid.uuid4()
    response = test_client.post(
        "/memories",
        json={
            "scope": "user",
            "memory_type": "fact",
            "content": "x",
            "owner_user_id": str(victim),
        },
    )

    assert response.status_code == 201
    assert not hasattr(captured["data"], "owner_user_id")


def test_create_memory_rejects_an_organization_id_in_the_body(client, monkeypatch):
    test_client, actor = client
    captured: dict = {}

    async def fake_create(session, passed_actor, data):
        captured["actor"] = passed_actor
        return _memory(actor)

    monkeypatch.setattr(memory_router.memory_service, "create_memory", fake_create)

    other_org = uuid.uuid4()
    response = test_client.post(
        "/memories",
        json={
            "scope": "user",
            "memory_type": "fact",
            "content": "x",
            "organization_id": str(other_org),
        },
    )

    assert response.status_code == 201
    # The service is always handed the authenticated identity's org.
    assert captured["actor"].organization_id == actor.organization_id
    assert captured["actor"].organization_id != other_org


def test_list_memories_takes_no_organization_parameter(client, monkeypatch):
    """The endpoint that would make cross-tenant reads expressible does not
    exist: any `organization_id` query param is simply ignored."""
    test_client, actor = client
    seen: dict = {}

    async def fake_list(session, passed_actor, *, limit, offset):
        seen["organization_id"] = passed_actor.organization_id
        return [_memory(actor)]

    monkeypatch.setattr(memory_router.memory_service, "list_memories", fake_list)

    other_org = uuid.uuid4()
    response = test_client.get(f"/memories?organization_id={other_org}")

    assert response.status_code == 200
    assert seen["organization_id"] == actor.organization_id
    assert seen["organization_id"] != other_org


def test_get_memory_passes_the_id_through(client, monkeypatch):
    test_client, actor = client
    target = uuid.uuid4()
    captured: dict = {}

    async def fake_get(session, passed_actor, memory_id):
        captured["memory_id"] = memory_id
        return _memory(actor, id=memory_id)

    monkeypatch.setattr(memory_router.memory_service, "get_memory", fake_get)

    response = test_client.get(f"/memories/{target}")
    assert response.status_code == 200
    assert captured["memory_id"] == target


def test_update_memory_accepts_content_only(client, monkeypatch):
    """Scope is immutable -- `MemoryUpdate` has no `scope` field, so an
    update cannot silently turn a private memory into a shared one."""
    test_client, actor = client
    captured: dict = {}

    async def fake_update(session, passed_actor, memory_id, data):
        captured["data"] = data
        return _memory(actor, content=data.content)

    monkeypatch.setattr(memory_router.memory_service, "update_memory", fake_update)

    response = test_client.patch(
        f"/memories/{uuid.uuid4()}",
        json={"content": "new content", "scope": "project"},
    )

    assert response.status_code == 200
    assert captured["data"].content == "new content"
    assert not hasattr(captured["data"], "scope")


def test_delete_memory_reports_whether_it_deleted_or_was_already_gone(client, monkeypatch):
    test_client, _actor = client

    async def fake_delete_true(session, actor, memory_id):
        return True

    monkeypatch.setattr(memory_router.memory_service, "delete_memory", fake_delete_true)
    payload = test_client.delete(f"/memories/{uuid.uuid4()}").json()
    assert payload["deleted"] is True

    async def fake_delete_false(session, actor, memory_id):
        return False

    monkeypatch.setattr(memory_router.memory_service, "delete_memory", fake_delete_false)
    payload = test_client.delete(f"/memories/{uuid.uuid4()}").json()
    assert payload["deleted"] is False
    assert "already" in payload["detail"].lower()


def test_no_endpoint_exposes_an_organization_scoped_memory_listing():
    """Guards the API shape itself: no memory route may take an organization
    or user id as a path parameter.

    Introspected via `app.openapi()["paths"]`, not `app.routes`: this
    FastAPI version represents `include_router` results as `_IncludedRouter`
    objects with no flat `.path`, so walking `app.routes` finds no endpoint
    paths at all and any assertion over it would pass vacuously.
    """
    paths = api_main.app.openapi()["paths"]
    memory_paths = [path for path in paths if path.startswith("/memories")]

    assert memory_paths, "memory routes should be registered"
    for path in memory_paths:
        assert "{organization_id}" not in path
        assert "{user_id}" not in path


def test_memory_exposes_exactly_the_intended_operations():
    """Locks the surface down: a future addition has to be deliberate."""
    paths = api_main.app.openapi()["paths"]
    operations = {
        (path, method.upper())
        for path, methods in paths.items()
        if path.startswith("/memories")
        for method in methods
    }
    assert operations == {
        ("/memories", "POST"),
        ("/memories", "GET"),
        ("/memories/{memory_id}", "GET"),
        ("/memories/{memory_id}", "PATCH"),
        ("/memories/{memory_id}", "DELETE"),
    }
