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

from app.core.exceptions import PermissionDeniedError
from app.core.incidents import service as incidents_service
from app.core.incidents.schemas import Postmortem
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
    def __init__(self, project_id: uuid.UUID | None) -> None:
        self.project_id = project_id


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
