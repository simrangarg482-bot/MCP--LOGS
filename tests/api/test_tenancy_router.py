"""Tests for `app.api.routers.tenancy` -- same `TestClient` +
`dependency_overrides` + stubbed-service style as
`tests/api/test_knowledge_router.py`/`test_observability_router.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import main as api_main
from app.api.deps import get_arq_pool, get_current_identity
from app.api.routers import tenancy as tenancy_router
from app.core.auth.schemas import SessionTokens
from app.core.tenancy.schemas import (
    AccessRule,
    ConnectorConfig,
    IngestionRun,
    Invitation,
    Organization,
    Project,
    SSOConfiguration,
)
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


def _connector_config(organization_id: uuid.UUID, *, source: str = "jira") -> ConnectorConfig:
    now = datetime.now(timezone.utc)
    return ConnectorConfig(
        id=uuid.uuid4(),
        organization_id=organization_id,
        project_id=None,
        source=source,
        credential_ref="encrypted-envelope-blob",
        config={},
        status="connecting",
        last_synced_at=None,
        created_at=now,
        updated_at=now,
    )


def test_register_connector_calls_service_with_callers_own_organization(client, monkeypatch) -> None:
    test_client, actor = client
    result_row = _connector_config(actor.organization_id)
    captured: dict[str, object] = {}

    async def fake_register_connector(session, passed_actor, organization_id, data):
        captured["actor"] = passed_actor
        captured["organization_id"] = organization_id
        captured["data"] = data
        return result_row

    monkeypatch.setattr(tenancy_router.tenancy_service, "register_connector", fake_register_connector)

    response = test_client.post(
        "/tenancy/connectors",
        json={"source": "jira", "credential_ref": "plaintext-api-token"},
    )

    assert response.status_code == 201
    assert response.json()["source"] == "jira"
    # No organization_id in the request body/path at all -- the endpoint
    # always uses the caller's own token-resolved organization, never a
    # client-suppliable one (matches every other router's convention).
    assert captured["actor"] is actor
    assert captured["organization_id"] == actor.organization_id
    assert captured["data"].credential_ref == "plaintext-api-token"


def test_register_connector_rejects_missing_required_fields(client) -> None:
    test_client, _actor = client

    response = test_client.post("/tenancy/connectors", json={})

    assert response.status_code == 422


def test_list_connectors_returns_callers_own_organization_configs(client, monkeypatch) -> None:
    test_client, actor = client
    rows = [_connector_config(actor.organization_id, source="jira"), _connector_config(actor.organization_id, source="teams")]

    async def fake_list_connectors(session, passed_actor, organization_id):
        assert passed_actor is actor
        assert organization_id == actor.organization_id
        return rows

    monkeypatch.setattr(tenancy_router.tenancy_service, "list_connectors", fake_list_connectors)

    response = test_client.get("/tenancy/connectors")

    assert response.status_code == 200
    sources = {item["source"] for item in response.json()}
    assert sources == {"jira", "teams"}


def test_delete_connector_calls_disconnect_with_callers_own_organization(client, monkeypatch) -> None:
    test_client, actor = client
    connector_id = uuid.uuid4()
    disconnected = _connector_config(actor.organization_id, source="github").model_copy(
        update={"status": "disconnected"}
    )
    captured: dict[str, object] = {}

    async def fake_disconnect_connector(session, passed_actor, organization_id, passed_connector_id):
        captured["actor"] = passed_actor
        captured["organization_id"] = organization_id
        captured["connector_id"] = passed_connector_id
        return disconnected

    monkeypatch.setattr(tenancy_router.tenancy_service, "disconnect_connector", fake_disconnect_connector)

    response = test_client.delete(f"/tenancy/connectors/{connector_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "disconnected"
    assert captured["actor"] is actor
    assert captured["organization_id"] == actor.organization_id
    assert captured["connector_id"] == connector_id


def test_connector_event_is_enqueued_idempotently(client, monkeypatch) -> None:
    test_client, actor = client
    connector = _connector_config(actor.organization_id)
    captured: dict[str, object] = {}

    async def fake_get_connector(session, passed_actor, organization_id, connector_id):
        assert passed_actor is actor
        assert organization_id == actor.organization_id
        return connector

    class _Pool:
        async def enqueue_job(self, function, connector_id, **kwargs):
            captured.update(function=function, connector_id=connector_id, kwargs=kwargs)
            return None  # ARQ returns None when the deterministic job id already exists.

    monkeypatch.setattr(tenancy_router.tenancy_service, "get_connector", fake_get_connector)
    api_main.app.dependency_overrides[get_arq_pool] = lambda: _Pool()

    response = test_client.post(
        f"/tenancy/connectors/{connector.id}/events",
        json={"event_id": "github-delivery-42", "event_type": "push"},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "duplicate"
    assert captured["function"] == "run_ingestion_job_task"
    assert captured["connector_id"] == str(connector.id)
    assert captured["kwargs"]["_job_id"].startswith(f"ingestion-event:{connector.id}:")


def test_dead_lettered_run_can_be_replayed(client, monkeypatch) -> None:
    test_client, actor = client
    connector = _connector_config(actor.organization_id)
    now = datetime.now(timezone.utc)
    run = IngestionRun(
        id=uuid.uuid4(),
        organization_id=actor.organization_id,
        connector_config_id=connector.id,
        status="dead_lettered",
        failed_stage="fetch",
        documents_processed=3,
        started_at=now,
        completed_at=now,
        created_at=now,
        updated_at=now,
    )
    enqueued: list[tuple[str, str, dict]] = []

    async def fake_get_replayable(session, passed_actor, organization_id, connector_id, job_id):
        assert passed_actor is actor
        assert organization_id == actor.organization_id
        assert connector_id == connector.id
        assert job_id == run.id
        return run

    class _Pool:
        async def enqueue_job(self, function, connector_id, **kwargs):
            enqueued.append((function, connector_id, kwargs))
            return object()

    monkeypatch.setattr(
        tenancy_router.tenancy_service,
        "get_replayable_ingestion_run",
        fake_get_replayable,
    )
    api_main.app.dependency_overrides[get_arq_pool] = lambda: _Pool()

    response = test_client.post(
        f"/tenancy/connectors/{connector.id}/runs/{run.id}/replay"
    )

    assert response.status_code == 202
    assert response.json()["replayed_job_id"] == str(run.id)
    assert enqueued == [
        (
            "run_ingestion_job_task",
            str(connector.id),
            {"_job_id": f"ingestion-replay:{run.id}"},
        )
    ]


# --- admin_router: organizations/projects/sso/access-rules/invitations -------


def _organization(organization_id: uuid.UUID) -> Organization:
    now = datetime.now(timezone.utc)
    return Organization(
        id=organization_id, name="Acme", slug="acme", status="onboarding",
        created_at=now, updated_at=now,
    )


def _project(organization_id: uuid.UUID) -> Project:
    now = datetime.now(timezone.utc)
    return Project(
        id=uuid.uuid4(), organization_id=organization_id, name="General",
        is_default=True, created_at=now, updated_at=now,
    )


def _sso_configuration(organization_id: uuid.UUID) -> SSOConfiguration:
    now = datetime.now(timezone.utc)
    return SSOConfiguration(
        id=uuid.uuid4(), organization_id=organization_id, provider="okta", protocol="oidc",
        issuer_url="https://acme.okta.com", client_id="client-123",
        client_secret_ref="encrypted-ref", created_at=now, updated_at=now,
    )


def _access_rule(organization_id: uuid.UUID) -> AccessRule:
    now = datetime.now(timezone.utc)
    return AccessRule(
        id=uuid.uuid4(), organization_id=organization_id, rule_type="domain", value="acme.com",
        grants_role_id=uuid.uuid4(), is_active=True, created_at=now, updated_at=now,
    )


def _invitation(organization_id: uuid.UUID, **overrides: object) -> Invitation:
    now = datetime.now(timezone.utc)
    defaults: dict[str, object] = dict(
        id=uuid.uuid4(), organization_id=organization_id, email="new.hire@acme.com",
        status="pending", grants_role_id=uuid.uuid4(), invited_by=uuid.uuid4(),
        expires_at=now, accepted_at=None, created_at=now, updated_at=now,
    )
    defaults.update(overrides)
    return Invitation(**defaults)


def test_create_organization_passes_actor_through(client, monkeypatch) -> None:
    test_client, actor = client
    result = _organization(uuid.uuid4())
    captured: dict[str, object] = {}

    async def fake_create_organization(session, data, actor=None):
        captured["actor"] = actor
        captured["data"] = data
        return result

    monkeypatch.setattr(tenancy_router.tenancy_service, "create_organization", fake_create_organization)

    response = test_client.post("/organizations", json={"name": "Acme", "slug": "acme"})

    assert response.status_code == 201
    assert captured["actor"] is actor


def test_list_organizations_returns_only_callers_own_organization(client, monkeypatch) -> None:
    """Confirms this endpoint does NOT call the unscoped
    `list_organizations` -- it calls `get_organization` for the caller's own
    org and wraps it in a single-element list (see router docstring).
    """
    test_client, actor = client
    result = _organization(actor.organization_id)

    async def fake_get_organization(session, passed_actor, organization_id):
        assert passed_actor is actor
        assert organization_id == actor.organization_id
        return result

    monkeypatch.setattr(tenancy_router.tenancy_service, "get_organization", fake_get_organization)

    response = test_client.get("/organizations")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["id"] == str(actor.organization_id)


def test_get_organization_by_id(client, monkeypatch) -> None:
    test_client, actor = client
    result = _organization(actor.organization_id)

    async def fake_get_organization(session, passed_actor, organization_id):
        return result

    monkeypatch.setattr(tenancy_router.tenancy_service, "get_organization", fake_get_organization)

    response = test_client.get(f"/organizations/{actor.organization_id}")

    assert response.status_code == 200
    assert response.json()["id"] == str(actor.organization_id)


def test_create_project_under_organization(client, monkeypatch) -> None:
    test_client, actor = client
    result = _project(actor.organization_id)
    captured: dict[str, object] = {}

    async def fake_create_project(session, passed_actor, organization_id, data):
        captured["organization_id"] = organization_id
        captured["data"] = data
        return result

    monkeypatch.setattr(tenancy_router.tenancy_service, "create_project", fake_create_project)

    response = test_client.post(
        f"/organizations/{actor.organization_id}/projects", json={"name": "Payments"}
    )

    assert response.status_code == 201
    assert captured["organization_id"] == actor.organization_id
    assert captured["data"].name == "Payments"


def test_list_projects_under_organization(client, monkeypatch) -> None:
    test_client, actor = client
    rows = [_project(actor.organization_id)]

    async def fake_list_projects(session, passed_actor, organization_id):
        return rows

    monkeypatch.setattr(tenancy_router.tenancy_service, "list_projects", fake_list_projects)

    response = test_client.get(f"/organizations/{actor.organization_id}/projects")

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_get_sso_config_under_organization(client, monkeypatch) -> None:
    test_client, actor = client
    result = _sso_configuration(actor.organization_id)
    captured: dict[str, object] = {}

    async def fake_get_sso_config(session, passed_actor, organization_id):
        captured["organization_id"] = organization_id
        return result

    monkeypatch.setattr(tenancy_router.tenancy_service, "get_sso_config", fake_get_sso_config)

    response = test_client.get(f"/organizations/{actor.organization_id}/sso")

    assert response.status_code == 200
    assert captured["organization_id"] == actor.organization_id
    assert response.json()["client_secret_ref"] == "encrypted-ref"


def test_configure_sso_under_organization(client, monkeypatch) -> None:
    test_client, actor = client
    result = _sso_configuration(actor.organization_id)
    captured: dict[str, object] = {}

    async def fake_configure_sso(session, passed_actor, organization_id, data):
        captured["data"] = data
        return result

    monkeypatch.setattr(tenancy_router.tenancy_service, "configure_sso", fake_configure_sso)

    response = test_client.post(
        f"/organizations/{actor.organization_id}/sso/configure",
        json={
            "provider": "okta",
            "issuer_url": "https://acme.okta.com",
            "client_id": "client-123",
            "client_secret_ref": "secret-ref",
        },
    )

    assert response.status_code == 201
    assert captured["data"].provider == "okta"


def test_create_access_rule_under_organization(client, monkeypatch) -> None:
    test_client, actor = client
    result = _access_rule(actor.organization_id)

    async def fake_create_access_rule(session, passed_actor, organization_id, data):
        return result

    monkeypatch.setattr(tenancy_router.tenancy_service, "create_access_rule", fake_create_access_rule)

    response = test_client.post(
        f"/organizations/{actor.organization_id}/access-rules",
        json={"rule_type": "domain", "value": "acme.com", "grants_role": "engineer"},
    )

    assert response.status_code == 201


def test_list_access_rules_under_organization(client, monkeypatch) -> None:
    test_client, actor = client
    rows = [_access_rule(actor.organization_id)]

    async def fake_list_access_rules(session, passed_actor, organization_id):
        return rows

    monkeypatch.setattr(tenancy_router.tenancy_service, "list_access_rules", fake_list_access_rules)

    response = test_client.get(f"/organizations/{actor.organization_id}/access-rules")

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_deactivate_access_rule_uses_callers_own_organization(client, monkeypatch) -> None:
    """No `{organization_id}` in this path -- confirms the handler always
    uses `actor.organization_id`, matching `router`'s connector endpoints.
    """
    test_client, actor = client
    rule_id = uuid.uuid4()
    result = _access_rule(actor.organization_id)
    captured: dict[str, object] = {}

    async def fake_deactivate_access_rule(session, passed_actor, organization_id, passed_rule_id):
        captured["organization_id"] = organization_id
        captured["rule_id"] = passed_rule_id
        return result

    monkeypatch.setattr(
        tenancy_router.tenancy_service, "deactivate_access_rule", fake_deactivate_access_rule
    )

    response = test_client.patch(f"/access-rules/{rule_id}/deactivate")

    assert response.status_code == 200
    assert captured["organization_id"] == actor.organization_id
    assert captured["rule_id"] == rule_id


def test_create_invitation_under_organization(client, monkeypatch) -> None:
    test_client, actor = client
    result = _invitation(actor.organization_id)

    async def fake_create_invitation(session, passed_actor, organization_id, data):
        return result

    monkeypatch.setattr(tenancy_router.tenancy_service, "create_invitation", fake_create_invitation)

    response = test_client.post(
        f"/organizations/{actor.organization_id}/invitations",
        json={"email": "new.hire@acme.com", "grants_role": "engineer"},
    )

    assert response.status_code == 201


def test_list_invitations_under_organization(client, monkeypatch) -> None:
    test_client, actor = client
    rows = [_invitation(actor.organization_id)]

    async def fake_list_invitations(session, passed_actor, organization_id):
        return rows

    monkeypatch.setattr(tenancy_router.tenancy_service, "list_invitations", fake_list_invitations)

    response = test_client.get(f"/organizations/{actor.organization_id}/invitations")

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_accept_invitation_requires_no_authentication(client, monkeypatch) -> None:
    """Deliberately unauthenticated -- see router docstring. Uses the same
    `client` fixture purely for its `TestClient`; the identity override in
    play is simply never consulted by this endpoint.

    Phase 7.5: the endpoint now delegates to `auth_service.
    accept_invitation_with_password` (not `tenancy_service.accept_invitation`
    directly) and requires a `token`/`password` body, returning `SessionTokens`
    rather than 204.
    """
    test_client, _actor = client
    invitation_id = uuid.uuid4()
    captured: dict[str, object] = {}

    async def fake_accept_invitation_with_password(session, passed_invitation_id, data):
        captured["invitation_id"] = passed_invitation_id
        captured["data"] = data
        return SessionTokens(
            access_token="access-token",
            refresh_token="refresh-token",
            expires_in=900,
        )

    monkeypatch.setattr(
        tenancy_router.auth_service,
        "accept_invitation_with_password",
        fake_accept_invitation_with_password,
    )

    response = test_client.post(
        f"/invitations/{invitation_id}/accept",
        json={"token": "raw-token", "password": "correct horse battery staple"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"] == "access-token"
    assert body["refresh_token"] == "refresh-token"
    assert captured["invitation_id"] == invitation_id
    assert captured["data"].token == "raw-token"
    assert captured["data"].password == "correct horse battery staple"


def test_revoke_invitation_uses_callers_own_organization(client, monkeypatch) -> None:
    test_client, actor = client
    invitation_id = uuid.uuid4()
    result = _invitation(actor.organization_id, status="revoked")
    captured: dict[str, object] = {}

    async def fake_revoke_invitation(session, passed_actor, organization_id, passed_invitation_id):
        captured["organization_id"] = organization_id
        return result

    monkeypatch.setattr(tenancy_router.tenancy_service, "revoke_invitation", fake_revoke_invitation)

    response = test_client.post(f"/invitations/{invitation_id}/revoke")

    assert response.status_code == 200
    assert response.json()["status"] == "revoked"
    assert captured["organization_id"] == actor.organization_id
