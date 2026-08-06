"""Tests for `app.api.routers.auth`'s `POST /auth/logout-all` -- not a full
test suite for the auth router (no test infrastructure for it existed
before this addition; the real SSO/PKCE flow already has its own coverage
in `tests/core/auth/test_service.py`). Same `TestClient` +
`dependency_overrides` + stubbed-service style as
`tests/api/test_tenancy_router.py`.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api import main as api_main
from app.api.deps import get_current_identity
from app.api.routers import auth as auth_router
from app.database.session import get_db_session
from app.shared.schemas import ActorKind, Identity


def _user(organization_id: uuid.UUID) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
    )


@pytest.fixture()
def client():
    organization_id = uuid.uuid4()
    actor = _user(organization_id)

    async def _fake_session():
        yield None

    api_main.app.dependency_overrides[get_current_identity] = lambda: actor
    api_main.app.dependency_overrides[get_db_session] = _fake_session

    yield TestClient(api_main.app), actor

    api_main.app.dependency_overrides.clear()


def test_logout_all_sessions_revokes_own_sessions_and_records_audit_event(client, monkeypatch) -> None:
    test_client, actor = client
    captured: dict[str, object] = {}

    async def fake_revoke_all_sessions(session, user_id, organization_id):
        captured["user_id"] = user_id
        captured["organization_id"] = organization_id
        return 3

    async def fake_record_audit_event(session, event_actor, **kwargs):
        captured["audit_actor"] = event_actor
        captured["audit_kwargs"] = kwargs

    monkeypatch.setattr(auth_router.auth_service, "revoke_all_sessions", fake_revoke_all_sessions)
    monkeypatch.setattr(auth_router, "record_audit_event", fake_record_audit_event)

    response = test_client.post("/auth/logout-all")

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Successfully logged out from all sessions"
    assert body["revoked_session_count"] == 3
    assert captured["user_id"] == actor.user_id
    assert captured["organization_id"] == actor.organization_id
    assert captured["audit_actor"] is actor
    assert captured["audit_kwargs"]["action"] == "user.logout_all_sessions"


def test_logout_all_sessions_rejects_non_user_identity(monkeypatch) -> None:
    """A service/agent identity has no sessions of its own -- same guard as
    `GET /auth/me`.
    """
    organization_id = uuid.uuid4()
    agent_actor = Identity.for_agent("some_agent", organization_id)

    async def _fake_session():
        yield None

    api_main.app.dependency_overrides[get_current_identity] = lambda: agent_actor
    api_main.app.dependency_overrides[get_db_session] = _fake_session
    try:
        test_client = TestClient(api_main.app)
        response = test_client.post("/auth/logout-all")
        assert response.status_code == 400
    finally:
        api_main.app.dependency_overrides.clear()
