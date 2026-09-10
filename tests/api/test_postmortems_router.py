"""Tests for `app.api.routers.postmortems` -- same `TestClient` +
`dependency_overrides` + stubbed-service style as
`tests/api/test_knowledge_router.py`/`test_tenancy_router.py`.

Focused on `POST /postmortems/{id}/approve`'s runbooks-ingestion
auto-trigger (org-knowledge/investigation feedback-loop plan, priority
P1): approval itself is always stubbed to succeed (its own status-
transition/permission behavior is `core.incidents.service`'s, already
covered elsewhere) -- these tests only verify the connector lookup +
`arq` enqueue this router adds around that call, and that nothing about
it can ever turn a successful approval into a failed response.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import main as api_main
from app.api.deps import get_arq_pool, get_current_identity
from app.api.routers import postmortems as postmortems_router
from app.core.incidents.schemas import Postmortem
from app.core.tenancy.schemas import ConnectorConfig
from app.database.session import get_db_session
from app.shared.schemas import ActorKind, Identity


def _approver(organization_id: uuid.UUID) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        permissions=frozenset({"postmortem:approve"}),
    )


def _postmortem(actor: Identity) -> Postmortem:
    now = datetime.now(timezone.utc)
    return Postmortem(
        id=uuid.uuid4(),
        organization_id=actor.organization_id,
        incident_id=uuid.uuid4(),
        status="approved",
        root_cause="A null discount configuration object.",
        action_items=[],
        generated_by="postmortem_agent",
        reviewed_by=actor.user_id,
        created_at=now,
        updated_at=now,
    )


def _connector(organization_id: uuid.UUID, *, source: str = "runbooks") -> ConnectorConfig:
    now = datetime.now(timezone.utc)
    return ConnectorConfig(
        id=uuid.uuid4(),
        organization_id=organization_id,
        project_id=None,
        source=source,
        credential_ref="encrypted-envelope-blob",
        config={},
        status="active",
        last_synced_at=None,
        created_at=now,
        updated_at=now,
    )


class _Pool:
    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self._raises = raises

    async def enqueue_job(self, function, connector_id, **kwargs):
        self.calls.append((function, connector_id))
        if self._raises:
            raise RuntimeError("redis unavailable")
        return object()


@pytest.fixture()
def client():
    organization_id = uuid.uuid4()
    actor = _approver(organization_id)

    async def _fake_session():
        yield None

    api_main.app.dependency_overrides[get_current_identity] = lambda: actor
    api_main.app.dependency_overrides[get_db_session] = _fake_session

    yield TestClient(api_main.app), actor

    api_main.app.dependency_overrides.clear()


def test_approve_postmortem_enqueues_runbooks_ingestion_when_connector_exists(client, monkeypatch) -> None:
    """Requirement 1: approving a postmortem, with a runbooks connector
    registered for the org, enqueues `run_ingestion_job_task` for it.
    """
    test_client, actor = client
    postmortem = _postmortem(actor)
    connector = _connector(actor.organization_id)
    pool = _Pool()

    async def fake_approve_postmortem(session, passed_actor, organization_id, postmortem_id):
        assert passed_actor is actor
        return postmortem

    async def fake_get_connector_by_source(session, organization_id, source):
        assert organization_id == actor.organization_id
        assert source == "runbooks"
        return connector

    monkeypatch.setattr(postmortems_router.incidents_service, "approve_postmortem", fake_approve_postmortem)
    monkeypatch.setattr(
        postmortems_router.tenancy_service, "get_connector_by_source", fake_get_connector_by_source
    )
    api_main.app.dependency_overrides[get_arq_pool] = lambda: pool

    response = test_client.post(f"/postmortems/{postmortem.id}/approve")

    assert response.status_code == 200
    assert response.json()["id"] == str(postmortem.id)
    assert pool.calls == [("run_ingestion_job_task", str(connector.id))]


def test_approve_postmortem_succeeds_with_no_runbooks_connector(client, monkeypatch) -> None:
    """Requirement 2: no runbooks connector registered for the org ->
    approval still succeeds, and nothing is enqueued.
    """
    test_client, actor = client
    postmortem = _postmortem(actor)
    pool = _Pool()

    async def fake_approve_postmortem(session, passed_actor, organization_id, postmortem_id):
        return postmortem

    async def fake_get_connector_by_source(session, organization_id, source):
        return None

    monkeypatch.setattr(postmortems_router.incidents_service, "approve_postmortem", fake_approve_postmortem)
    monkeypatch.setattr(
        postmortems_router.tenancy_service, "get_connector_by_source", fake_get_connector_by_source
    )
    api_main.app.dependency_overrides[get_arq_pool] = lambda: pool

    response = test_client.post(f"/postmortems/{postmortem.id}/approve")

    assert response.status_code == 200
    assert response.json()["id"] == str(postmortem.id)
    assert pool.calls == []


def test_approve_postmortem_succeeds_when_enqueue_fails(client, monkeypatch) -> None:
    """Requirement 3: the enqueue call itself raising must not fail
    approval -- the response is still the successfully-approved postmortem.
    """
    test_client, actor = client
    postmortem = _postmortem(actor)
    connector = _connector(actor.organization_id)
    pool = _Pool(raises=True)

    async def fake_approve_postmortem(session, passed_actor, organization_id, postmortem_id):
        return postmortem

    async def fake_get_connector_by_source(session, organization_id, source):
        return connector

    monkeypatch.setattr(postmortems_router.incidents_service, "approve_postmortem", fake_approve_postmortem)
    monkeypatch.setattr(
        postmortems_router.tenancy_service, "get_connector_by_source", fake_get_connector_by_source
    )
    api_main.app.dependency_overrides[get_arq_pool] = lambda: pool

    response = test_client.post(f"/postmortems/{postmortem.id}/approve")

    assert response.status_code == 200
    assert response.json()["id"] == str(postmortem.id)
    assert pool.calls == [("run_ingestion_job_task", str(connector.id))]  # attempted, then swallowed


def test_approve_postmortem_succeeds_when_connector_lookup_raises(client, monkeypatch) -> None:
    """Same protection as the enqueue-failure case above, for a failure one
    step earlier: the connector lookup itself raising.
    """
    test_client, actor = client
    postmortem = _postmortem(actor)
    pool = _Pool()

    async def fake_approve_postmortem(session, passed_actor, organization_id, postmortem_id):
        return postmortem

    async def failing_get_connector_by_source(session, organization_id, source):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(postmortems_router.incidents_service, "approve_postmortem", fake_approve_postmortem)
    monkeypatch.setattr(
        postmortems_router.tenancy_service, "get_connector_by_source", failing_get_connector_by_source
    )
    api_main.app.dependency_overrides[get_arq_pool] = lambda: pool

    response = test_client.post(f"/postmortems/{postmortem.id}/approve")

    assert response.status_code == 200
    assert response.json()["id"] == str(postmortem.id)
    assert pool.calls == []


def test_approve_postmortem_selects_the_callers_own_organization(client, monkeypatch) -> None:
    """Requirement 4: the connector lookup is scoped to the approving
    actor's own organization, not some other one -- guards against a
    future refactor accidentally passing the wrong organization_id through.
    """
    test_client, actor = client
    postmortem = _postmortem(actor)
    connector = _connector(actor.organization_id)
    pool = _Pool()
    captured: dict[str, object] = {}

    async def fake_approve_postmortem(session, passed_actor, organization_id, postmortem_id):
        return postmortem

    async def fake_get_connector_by_source(session, organization_id, source):
        captured["organization_id"] = organization_id
        captured["source"] = source
        return connector

    monkeypatch.setattr(postmortems_router.incidents_service, "approve_postmortem", fake_approve_postmortem)
    monkeypatch.setattr(
        postmortems_router.tenancy_service, "get_connector_by_source", fake_get_connector_by_source
    )
    api_main.app.dependency_overrides[get_arq_pool] = lambda: pool

    test_client.post(f"/postmortems/{postmortem.id}/approve")

    assert captured == {"organization_id": actor.organization_id, "source": "runbooks"}
