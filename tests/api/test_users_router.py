"""Tests for `app.api.routers.users` -- the admin-triggered
`POST /users/{user_id}/logout-all`. Same `TestClient` +
`dependency_overrides` + stubbed-service style as
`tests/api/test_tenancy_router.py`.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api import main as api_main
from app.api.deps import get_current_identity
from app.api.routers import users as users_router
from app.database.session import get_db_session
from app.shared.schemas import ActorKind, Identity


def _admin(organization_id: uuid.UUID) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        permissions=frozenset({"tenancy:manage"}),
    )


def _non_admin(organization_id: uuid.UUID) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
    )


@pytest.fixture()
def client():
    organization_id = uuid.uuid4()
    actor = _admin(organization_id)

    async def _fake_session():
        yield None

    api_main.app.dependency_overrides[get_current_identity] = lambda: actor
    api_main.app.dependency_overrides[get_db_session] = _fake_session

    yield TestClient(api_main.app), actor

    api_main.app.dependency_overrides.clear()


def test_admin_can_revoke_another_users_sessions(client, monkeypatch) -> None:
    test_client, actor = client
    target_user_id = uuid.uuid4()
    captured: dict[str, object] = {}

    async def fake_revoke_all_sessions(session, user_id, organization_id):
        captured["user_id"] = user_id
        captured["organization_id"] = organization_id
        return 2

    async def fake_record_audit_event(session, event_actor, **kwargs):
        captured["audit_kwargs"] = kwargs

    monkeypatch.setattr(users_router.auth_service, "revoke_all_sessions", fake_revoke_all_sessions)
    monkeypatch.setattr(users_router, "record_audit_event", fake_record_audit_event)

    response = test_client.post(f"/users/{target_user_id}/logout-all")

    assert response.status_code == 200
    body = response.json()
    assert body["revoked_session_count"] == 2
    assert captured["user_id"] == target_user_id
    assert captured["organization_id"] == actor.organization_id
    assert captured["audit_kwargs"]["metadata"]["revoked_by_admin"] is True


def test_non_admin_cannot_revoke_another_users_sessions() -> None:
    organization_id = uuid.uuid4()
    non_admin_actor = _non_admin(organization_id)
    target_user_id = uuid.uuid4()

    async def _fake_session():
        yield None

    api_main.app.dependency_overrides[get_current_identity] = lambda: non_admin_actor
    api_main.app.dependency_overrides[get_db_session] = _fake_session
    try:
        test_client = TestClient(api_main.app)
        response = test_client.post(f"/users/{target_user_id}/logout-all")
        assert response.status_code == 403
    finally:
        api_main.app.dependency_overrides.clear()
