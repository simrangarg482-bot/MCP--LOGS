"""Tests for `app.core.incidents.service.list_postmortems_for_ingestion` --
the Milestone 9 addition backing `app.ingestion.connectors.runbooks`. Not a
full test suite for `core.incidents.service` (no test infrastructure for
that module existed before this addition) -- scoped to the one new function,
following the same "monkeypatch the repository call" style already
established in `tests/agents/test_service.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.incidents import service as incidents_service
from app.core.incidents.schemas import (
    Incident,
    IncidentFilter,
    IncidentUpdate,
    Postmortem,
    TimelineEntry,
)
from app.shared.schemas import ActorKind, Identity


def _postmortem_row(organization_id: uuid.UUID, **overrides: object) -> Postmortem:
    now = datetime.now(timezone.utc)
    defaults: dict[str, object] = dict(
        id=uuid.uuid4(),
        organization_id=organization_id,
        incident_id=uuid.uuid4(),
        status="approved",
        root_cause="A null pointer in the checkout handler.",
        action_items=[],
        generated_by="agent:postmortem_agent",
        reviewed_by=uuid.uuid4(),
        created_at=now,
        updated_at=now,
    )
    defaults.update(overrides)
    return Postmortem(**defaults)


@pytest.mark.asyncio
async def test_list_postmortems_for_ingestion_passes_through_and_maps(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    row = _postmortem_row(organization_id)
    captured: dict[str, object] = {}

    async def fake_repo(session, org_id, *, statuses, since, offset, limit):
        captured["organization_id"] = org_id
        captured["statuses"] = statuses
        captured["since"] = since
        captured["offset"] = offset
        captured["limit"] = limit
        return [row]

    monkeypatch.setattr(
        incidents_service.repository, "list_postmortems_for_ingestion", fake_repo
    )

    since = datetime(2026, 7, 1, tzinfo=timezone.utc)
    result = await incidents_service.list_postmortems_for_ingestion(
        None, organization_id, since=since, offset=10, limit=25
    )

    assert result == [row]
    assert captured["organization_id"] == organization_id
    assert captured["statuses"] == ("approved", "published")
    assert captured["since"] == since
    assert captured["offset"] == 10
    assert captured["limit"] == 25


@pytest.mark.asyncio
async def test_list_postmortems_for_ingestion_requires_no_actor(monkeypatch) -> None:
    """No `actor: Identity` parameter exists on this function at all -- this
    test exists mainly to pin that signature choice down: calling it with
    only `(session, organization_id, since=, offset=, limit=)` must succeed,
    unlike every actor-gated function in this module.
    """
    organization_id = uuid.uuid4()

    async def fake_repo(session, org_id, *, statuses, since, offset, limit):
        return []

    monkeypatch.setattr(
        incidents_service.repository, "list_postmortems_for_ingestion", fake_repo
    )

    result = await incidents_service.list_postmortems_for_ingestion(
        None, organization_id, since=None, offset=0, limit=50
    )

    assert result == []


class _FakeIncidentRow:
    def __init__(self, project_id: uuid.UUID | None, organization_id: uuid.UUID | None = None) -> None:
        self.project_id = project_id
        self.organization_id = organization_id


def _member_no_permissions(organization_id: uuid.UUID) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_get_postmortem_denies_draft_without_permission(monkeypatch) -> None:
    """A draft postmortem's unreviewed root-cause analysis must not be
    readable by an org member holding neither `postmortem:write` nor
    `postmortem:approve` -- previously this function had no permission
    check at all for any status, including draft.
    """
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    row = _postmortem_row(organization_id, status="draft")
    actor = _member_no_permissions(organization_id)

    async def fake_get_postmortem_by_id(session, postmortem_id):
        return row

    async def fake_get_incident_by_id(session, incident_id):
        return _FakeIncidentRow(project_id)

    monkeypatch.setattr(
        incidents_service.repository, "get_postmortem_by_id", fake_get_postmortem_by_id
    )
    monkeypatch.setattr(
        incidents_service.repository, "get_incident_by_id", fake_get_incident_by_id
    )

    with pytest.raises(PermissionDeniedError):
        await incidents_service.get_postmortem(None, actor, organization_id, row.id)


@pytest.mark.asyncio
async def test_get_postmortem_allows_draft_with_write_permission(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    row = _postmortem_row(organization_id, status="draft")
    actor = Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        project_permissions={project_id: frozenset({"postmortem:write"})},
    )

    async def fake_get_postmortem_by_id(session, postmortem_id):
        return row

    async def fake_get_incident_by_id(session, incident_id):
        return _FakeIncidentRow(project_id)

    monkeypatch.setattr(
        incidents_service.repository, "get_postmortem_by_id", fake_get_postmortem_by_id
    )
    monkeypatch.setattr(
        incidents_service.repository, "get_incident_by_id", fake_get_incident_by_id
    )

    result = await incidents_service.get_postmortem(None, actor, organization_id, row.id)
    assert result.id == row.id


@pytest.mark.asyncio
async def test_get_postmortem_allows_approved_without_permission(monkeypatch) -> None:
    """An already-reviewed postmortem (approved/published) is readable by
    any org member, no permission check needed -- mirroring `core.knowledge.
    service.get_document`'s published-vs-proposed gate.
    """
    organization_id = uuid.uuid4()
    row = _postmortem_row(organization_id, status="approved")
    actor = _member_no_permissions(organization_id)

    async def fake_get_postmortem_by_id(session, postmortem_id):
        return row

    monkeypatch.setattr(
        incidents_service.repository, "get_postmortem_by_id", fake_get_postmortem_by_id
    )

    result = await incidents_service.get_postmortem(None, actor, organization_id, row.id)
    assert result.id == row.id


@pytest.mark.asyncio
async def test_get_postmortem_by_incident_raises_not_found_when_none_exists(monkeypatch) -> None:
    """Regression test for the Phase 2D `GET /incidents/{id}/postmortem`
    addition -- a 404 here is the expected way a caller learns "no
    postmortem yet, offer to generate one," not a genuine error.
    """
    from app.core.exceptions import NotFoundError

    organization_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    actor = _member_no_permissions(organization_id)

    async def fake_get_incident_by_id(session, inc_id):
        return _FakeIncidentRow(uuid.uuid4(), organization_id=organization_id)

    async def fake_get_postmortem_by_incident_id(session, inc_id):
        return None

    monkeypatch.setattr(
        incidents_service.repository, "get_incident_by_id", fake_get_incident_by_id
    )
    monkeypatch.setattr(
        incidents_service.repository,
        "get_postmortem_by_incident_id",
        fake_get_postmortem_by_incident_id,
    )

    with pytest.raises(NotFoundError):
        await incidents_service.get_postmortem_by_incident(None, actor, organization_id, incident_id)


@pytest.mark.asyncio
async def test_get_postmortem_by_incident_denies_draft_without_permission(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    project_id = uuid.uuid4()
    row = _postmortem_row(organization_id, status="draft", incident_id=incident_id)
    actor = _member_no_permissions(organization_id)

    async def fake_get_incident_by_id(session, inc_id):
        return _FakeIncidentRow(project_id, organization_id=organization_id)

    async def fake_get_postmortem_by_incident_id(session, inc_id):
        return row

    monkeypatch.setattr(
        incidents_service.repository, "get_incident_by_id", fake_get_incident_by_id
    )
    monkeypatch.setattr(
        incidents_service.repository,
        "get_postmortem_by_incident_id",
        fake_get_postmortem_by_incident_id,
    )

    with pytest.raises(PermissionDeniedError):
        await incidents_service.get_postmortem_by_incident(None, actor, organization_id, incident_id)


@pytest.mark.asyncio
async def test_get_postmortem_by_incident_allows_approved_without_permission(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    row = _postmortem_row(organization_id, status="approved", incident_id=incident_id)
    actor = _member_no_permissions(organization_id)

    async def fake_get_incident_by_id(session, inc_id):
        return _FakeIncidentRow(uuid.uuid4(), organization_id=organization_id)

    async def fake_get_postmortem_by_incident_id(session, inc_id):
        return row

    monkeypatch.setattr(
        incidents_service.repository, "get_incident_by_id", fake_get_incident_by_id
    )
    monkeypatch.setattr(
        incidents_service.repository,
        "get_postmortem_by_incident_id",
        fake_get_postmortem_by_incident_id,
    )

    result = await incidents_service.get_postmortem_by_incident(None, actor, organization_id, incident_id)
    assert result.id == row.id


# --- Phase 4.7.2: incident:read authorization -------------------------------
# Regression coverage for the confirmed access-control gap (2026-08 audit
# "H4") where `get_incident`/`list_incidents`/`get_timeline` checked only
# same-organization membership, with no permission check at all -- see
# EKIP_TENANT_ISOLATION_SECURITY_REVIEW.md recommendation #3 and
# docs/operations/migration-recovery.md.


def _incident_row(organization_id: uuid.UUID, **overrides: object) -> Incident:
    now = datetime.now(timezone.utc)
    defaults: dict[str, object] = dict(
        id=uuid.uuid4(),
        organization_id=organization_id,
        project_id=uuid.uuid4(),
        title="Checkout service returning 500s",
        description="Elevated error rate on POST /checkout since 14:02 UTC.",
        status="investigating",
        severity="high",
        owner_team=None,
        reported_by=uuid.uuid4(),
        resolved_at=None,
        created_at=now,
        updated_at=now,
    )
    defaults.update(overrides)
    return Incident(**defaults)


def _timeline_row(organization_id: uuid.UUID, incident_id: uuid.UUID) -> TimelineEntry:
    return TimelineEntry(
        id=uuid.uuid4(),
        organization_id=organization_id,
        incident_id=incident_id,
        event_type="note",
        event_data={"text": "Rolled back the last deploy."},
        actor="user:1234",
        occurred_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_get_incident_denies_org_member_without_incident_read(monkeypatch) -> None:
    """Previously the only gate here was `_ensure_same_organization` -- any
    identity in the organization, including one with zero role assignments,
    could read the incident's full detail.
    """
    organization_id = uuid.uuid4()
    row = _incident_row(organization_id)
    actor = _member_no_permissions(organization_id)

    async def fake_get_incident_by_id(session, incident_id):
        return row

    monkeypatch.setattr(incidents_service.repository, "get_incident_by_id", fake_get_incident_by_id)

    with pytest.raises(PermissionDeniedError):
        await incidents_service.get_incident(None, actor, organization_id, row.id)


@pytest.mark.asyncio
async def test_get_incident_allows_with_project_scoped_incident_read(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    row = _incident_row(organization_id)
    actor = Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        project_permissions={row.project_id: frozenset({"incident:read"})},
    )

    async def fake_get_incident_by_id(session, incident_id):
        return row

    monkeypatch.setattr(incidents_service.repository, "get_incident_by_id", fake_get_incident_by_id)

    result = await incidents_service.get_incident(None, actor, organization_id, row.id)
    assert result.id == row.id


@pytest.mark.asyncio
async def test_get_incident_allows_with_org_level_incident_read(monkeypatch) -> None:
    """Org-level `permissions` is the fallback when no project-scoped
    override exists (`Identity.has_permission`'s own documented behavior) --
    this is what a backfilled/admin-style role looks like in practice.
    """
    organization_id = uuid.uuid4()
    row = _incident_row(organization_id)
    actor = Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        permissions=frozenset({"incident:read"}),
    )

    async def fake_get_incident_by_id(session, incident_id):
        return row

    monkeypatch.setattr(incidents_service.repository, "get_incident_by_id", fake_get_incident_by_id)

    result = await incidents_service.get_incident(None, actor, organization_id, row.id)
    assert result.id == row.id


@pytest.mark.asyncio
async def test_get_incident_cross_organization_denied_before_permission_check(monkeypatch) -> None:
    """A different organization's actor must be denied by
    `_ensure_same_organization` -- never even reaching the permission check,
    let alone leaking whether the incident exists.
    """
    organization_id = uuid.uuid4()
    other_organization_id = uuid.uuid4()
    row = _incident_row(organization_id)
    actor = Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=other_organization_id,
        user_id=uuid.uuid4(),
        permissions=frozenset({"incident:read"}),
    )

    async def fake_get_incident_by_id(session, incident_id):
        return row

    monkeypatch.setattr(incidents_service.repository, "get_incident_by_id", fake_get_incident_by_id)

    with pytest.raises(PermissionDeniedError):
        await incidents_service.get_incident(None, actor, organization_id, row.id)


@pytest.mark.asyncio
async def test_get_incident_nonexistent_id_is_not_found_regardless_of_permission(monkeypatch) -> None:
    """A nonexistent (or cross-organization) incident id 404s before any
    permission is evaluated -- never distinguishing "exists, denied" from
    "doesn't exist" to a caller who can't see the row at all.
    """
    organization_id = uuid.uuid4()
    actor = _member_no_permissions(organization_id)

    async def fake_get_incident_by_id(session, incident_id):
        return None

    monkeypatch.setattr(incidents_service.repository, "get_incident_by_id", fake_get_incident_by_id)

    with pytest.raises(NotFoundError):
        await incidents_service.get_incident(None, actor, organization_id, uuid.uuid4())


@pytest.mark.asyncio
async def test_list_incidents_denies_org_member_without_incident_read(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    actor = _member_no_permissions(organization_id)

    async def fake_list_incidents(session, org_id, query):
        return [_incident_row(organization_id)]

    monkeypatch.setattr(incidents_service.repository, "list_incidents", fake_list_incidents)

    with pytest.raises(PermissionDeniedError):
        await incidents_service.list_incidents(None, actor, organization_id, IncidentFilter())


@pytest.mark.asyncio
async def test_list_incidents_allows_with_org_level_incident_read(monkeypatch) -> None:
    """`list_incidents` has no per-incident project_id to scope against (no
    project filter on `IncidentFilter`), so this is always an org-level
    check, never a project-scoped one.
    """
    organization_id = uuid.uuid4()
    row = _incident_row(organization_id)
    actor = Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        permissions=frozenset({"incident:read"}),
    )

    async def fake_list_incidents(session, org_id, query):
        return [row]

    monkeypatch.setattr(incidents_service.repository, "list_incidents", fake_list_incidents)

    result = await incidents_service.list_incidents(None, actor, organization_id, IncidentFilter())
    assert [r.id for r in result] == [row.id]


@pytest.mark.asyncio
async def test_get_timeline_denies_org_member_without_incident_read(monkeypatch) -> None:
    """A timeline entry can include investigation evidence and root-cause
    detail (`record_investigation_result`) -- must not be readable by
    anyone who couldn't read the incident itself.
    """
    organization_id = uuid.uuid4()
    incident_row = _incident_row(organization_id)
    actor = _member_no_permissions(organization_id)

    async def fake_get_incident_by_id(session, incident_id):
        return incident_row

    async def fake_list_timeline_entries(session, incident_id):
        return [_timeline_row(organization_id, incident_id)]

    monkeypatch.setattr(incidents_service.repository, "get_incident_by_id", fake_get_incident_by_id)
    monkeypatch.setattr(
        incidents_service.repository, "list_timeline_entries", fake_list_timeline_entries
    )

    with pytest.raises(PermissionDeniedError):
        await incidents_service.get_timeline(None, actor, organization_id, incident_row.id)


@pytest.mark.asyncio
async def test_get_timeline_allows_with_project_scoped_incident_read(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    incident_row = _incident_row(organization_id)
    actor = Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        project_permissions={incident_row.project_id: frozenset({"incident:read"})},
    )

    async def fake_get_incident_by_id(session, incident_id):
        return incident_row

    async def fake_list_timeline_entries(session, incident_id):
        return [_timeline_row(organization_id, incident_id)]

    monkeypatch.setattr(incidents_service.repository, "get_incident_by_id", fake_get_incident_by_id)
    monkeypatch.setattr(
        incidents_service.repository, "list_timeline_entries", fake_list_timeline_entries
    )

    result = await incidents_service.get_timeline(None, actor, organization_id, incident_row.id)
    assert len(result) == 1


@pytest.mark.asyncio
async def test_get_timeline_cross_organization_denied(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    other_organization_id = uuid.uuid4()
    incident_row = _incident_row(organization_id)
    actor = Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=other_organization_id,
        user_id=uuid.uuid4(),
        permissions=frozenset({"incident:read"}),
    )

    async def fake_get_incident_by_id(session, incident_id):
        return incident_row

    monkeypatch.setattr(incidents_service.repository, "get_incident_by_id", fake_get_incident_by_id)

    with pytest.raises(PermissionDeniedError):
        await incidents_service.get_timeline(None, actor, organization_id, incident_row.id)


# --- update_incident: resolved_at bookkeeping / audit changed_fields --------


async def _run_update_incident(monkeypatch, *, existing_resolved_at, patch):
    """Drive `update_incident` with the repository/audit calls mocked,
    returning (repo_update_kwargs, audit_changed_fields).
    """
    organization_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    project_id = uuid.uuid4()
    actor = Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        permissions=frozenset({"incident:write"}),
    )
    existing = _incident_row(
        organization_id, id=incident_id, project_id=project_id, resolved_at=existing_resolved_at
    )
    captured: dict[str, object] = {}

    async def fake_get_incident_by_id(session, inc_id):
        return existing

    async def fake_update_incident(session, inc_id, **fields):
        captured["update_kwargs"] = fields
        return _incident_row(
            organization_id, id=incident_id, project_id=project_id, **fields
        )

    async def fake_record_audit_event(session, audit_actor, **kwargs):
        captured["changed_fields"] = kwargs["metadata"]["changed_fields"]

    monkeypatch.setattr(incidents_service.repository, "get_incident_by_id", fake_get_incident_by_id)
    monkeypatch.setattr(incidents_service.repository, "update_incident", fake_update_incident)
    monkeypatch.setattr(incidents_service, "record_audit_event", fake_record_audit_event)

    await incidents_service.update_incident(
        None, actor, organization_id, incident_id, IncidentUpdate(**patch)
    )
    return captured["update_kwargs"], captured["changed_fields"]


@pytest.mark.asyncio
async def test_update_incident_to_investigating_leaves_unset_resolved_at(monkeypatch) -> None:
    """Regression: patching an open incident to `investigating` used to write
    `resolved_at = NULL -> NULL` and list a phantom `resolved_at` in the
    audit event's `changed_fields`.
    """
    update_kwargs, changed_fields = await _run_update_incident(
        monkeypatch, existing_resolved_at=None, patch={"status": "investigating"}
    )
    assert "resolved_at" not in update_kwargs
    assert "resolved_at" not in changed_fields
    assert changed_fields == ["status"]


@pytest.mark.asyncio
async def test_update_incident_to_resolved_stamps_resolved_at(monkeypatch) -> None:
    update_kwargs, changed_fields = await _run_update_incident(
        monkeypatch, existing_resolved_at=None, patch={"status": "resolved"}
    )
    assert update_kwargs["resolved_at"] is not None
    assert set(changed_fields) == {"status", "resolved_at"}


@pytest.mark.asyncio
async def test_update_incident_reopening_clears_resolved_at(monkeypatch) -> None:
    resolved = datetime.now(timezone.utc)
    update_kwargs, changed_fields = await _run_update_incident(
        monkeypatch,
        existing_resolved_at=resolved,
        patch={"status": "open"},
    )
    assert update_kwargs["resolved_at"] is None
    assert set(changed_fields) == {"status", "resolved_at"}
