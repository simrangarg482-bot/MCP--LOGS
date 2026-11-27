"""`GET /observability/storage` (tenant) and `GET /platform/storage` (operator): permission gating and the
shape of what each audience may see (no database; the service is stubbed)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import main as api_main
from app.api.deps import get_current_identity
from app.api.routers import observability as observability_router
from app.core.storage.service import TenantStorage
from app.database.session import get_db_session
from app.shared.schemas import ActorKind, Identity


def _identity(*permissions: str) -> Identity:
    return Identity(
        kind=ActorKind.USER, subject=str(uuid.uuid4()), organization_id=uuid.uuid4(), user_id=uuid.uuid4(),
        permissions=frozenset(permissions),
    )


@pytest.fixture()
def client():
    async def _fake_session():
        yield None

    api_main.app.dependency_overrides[get_db_session] = _fake_session
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def test_tenant_storage_needs_observability_read(client) -> None:
    api_main.app.dependency_overrides[get_current_identity] = lambda: _identity("incident:read")
    response = client.get("/observability/storage")
    assert response.status_code == 403


def test_tenant_storage_returns_only_the_callers_own_estimate(client, monkeypatch) -> None:
    actor = _identity("observability:read")
    api_main.app.dependency_overrides[get_current_identity] = lambda: actor
    seen = {}

    async def fake(session, organization_id):
        seen["org"] = organization_id
        return TenantStorage(available=True, taken_at=datetime.now(timezone.utc), chunk_rows=10, document_rows=2, estimated_bytes=1234)

    monkeypatch.setattr(observability_router.storage_service, "get_tenant_storage", fake)
    response = client.get("/observability/storage")
    assert response.status_code == 200
    body = response.json()
    assert seen["org"] == actor.organization_id  # the organization comes from the identity, never a parameter
    assert body["is_estimate"] is True and body["basis"] == "row_share_of_chunk_and_document_tables"
    assert body["estimated_bytes"] == 1234 and body["chunk_rows"] == 10
    assert "database_bytes" not in body and "storage_limit_bytes" not in body  # nothing about the whole database
    assert client.get("/observability/storage", params={"organization_id": str(uuid.uuid4())}).json()["chunk_rows"] == 10  # parameter ignored


def test_platform_storage_is_operator_only(client) -> None:
    # No platform token: refused (404 while the kill switch is off, 401/403 when it is on) -- never data.
    response = client.get("/platform/storage")
    assert response.status_code in (401, 403, 404)
    response = client.get("/platform/storage", headers={"Authorization": "Bearer not-a-platform-token"})
    assert response.status_code in (401, 403, 404)
    # a tenant access token is not a platform credential
    api_main.app.dependency_overrides[get_current_identity] = lambda: _identity("tenancy:manage", "observability:read")
    assert client.get("/platform/storage").status_code in (401, 403, 404)
