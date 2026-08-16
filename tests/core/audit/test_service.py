"""Tests for `app.core.audit.service.query_audit_log`'s Phase 2C addition:
a new `audit:read` permission gate, added at the same time this function
got its first real REST caller (`GET /organizations/{id}/audit`) -- see
that function's own docstring for why an unrestricted-by-permission read
of the full org-wide audit trail would be a real gap the moment any
caller could reach it.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.audit import service as audit_service
from app.core.audit.schemas import AuditLogQuery
from app.core.exceptions import PermissionDeniedError
from app.shared.schemas import ActorKind, Identity


def _actor(organization_id: uuid.UUID, *, permissions: frozenset[str] = frozenset()) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        permissions=permissions,
    )


@pytest.mark.asyncio
async def test_query_audit_log_denies_without_audit_read_permission() -> None:
    organization_id = uuid.uuid4()
    actor = _actor(organization_id)

    with pytest.raises(PermissionDeniedError):
        await audit_service.query_audit_log(None, actor, organization_id, AuditLogQuery())


@pytest.mark.asyncio
async def test_query_audit_log_denies_cross_organization_before_checking_permission() -> None:
    """Tenant isolation is checked first -- an actor from a different
    organization is denied even if (hypothetically) they held `audit:read`
    somewhere, since that permission is only ever meaningful within their
    own organization.
    """
    actor = _actor(uuid.uuid4(), permissions=frozenset({"audit:read"}))
    other_organization_id = uuid.uuid4()

    with pytest.raises(PermissionDeniedError):
        await audit_service.query_audit_log(None, actor, other_organization_id, AuditLogQuery())


@pytest.mark.asyncio
async def test_query_audit_log_succeeds_with_audit_read_permission(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    actor = _actor(organization_id, permissions=frozenset({"audit:read"}))
    captured: dict[str, object] = {}

    async def fake_list_entries(session, org_id, query):
        captured["organization_id"] = org_id
        captured["query"] = query
        return []

    monkeypatch.setattr(audit_service.repository, "list_entries", fake_list_entries)

    query = AuditLogQuery(resource_type="incident", limit=10)
    result = await audit_service.query_audit_log(None, actor, organization_id, query)

    assert result == []
    assert captured["organization_id"] == organization_id
    assert captured["query"] is query
