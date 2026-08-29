"""Persistence for core/proactive -- pure data access on
`proactive_findings`/`proactive_finding_evidence`.

Owned by: core/proactive. What this module does NOT do is decide whether a
caller may see a finding or its evidence -- that decision needs each
evidence entity's own source table (`incidents`, `documents`) this module
has no reason to know about, and lives in `service.py`, which resolves and
authorizes every evidence row before ever returning it. This module's own
filtering is purely structural: organization, status, fingerprint identity
-- the same split `core.graph.repository`'s own docstring establishes.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete as sql_delete
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.pattern_models import ProactiveFinding, ProactiveFindingEvidence

_ACTIVE = "active"
_INACTIVE = "inactive"


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------


async def get_finding_by_fingerprint(
    session: AsyncSession, *, organization_id: uuid.UUID, fingerprint: str
) -> ProactiveFinding | None:
    stmt = select(ProactiveFinding).where(
        ProactiveFinding.organization_id == organization_id,
        ProactiveFinding.fingerprint == fingerprint,
    )
    return (await session.execute(stmt)).scalars().first()


async def get_finding(
    session: AsyncSession, finding_id: uuid.UUID, *, organization_id: uuid.UUID
) -> ProactiveFinding | None:
    """One finding, tenant-scoped, in ANY status -- authorization and
    caller-facing status filtering are `service.py`'s job, not this
    function's; a not-found-vs-invisible distinction that leaks nothing is
    only possible if the caller can first tell "does this id exist at all
    in my organization."
    """
    stmt = select(ProactiveFinding).where(
        ProactiveFinding.id == finding_id, ProactiveFinding.organization_id == organization_id
    )
    return (await session.execute(stmt)).scalars().first()


async def list_findings(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    status: str | None,
    finding_type: str | None,
    limit: int,
    offset: int,
) -> Sequence[ProactiveFinding]:
    """Tenant-scoped listing, newest-active-first. Permission filtering
    happens in `service.py`, in Python, against `Identity.
    project_permissions` -- there is no SQL-expressible predicate for that
    here, unlike `core.memory.repository`'s ownership-based visibility,
    since project permission is data on the caller's `Identity`, not a
    column on this row.
    """
    stmt = select(ProactiveFinding).where(ProactiveFinding.organization_id == organization_id)
    if status is not None:
        stmt = stmt.where(ProactiveFinding.status == status)
    if finding_type is not None:
        stmt = stmt.where(ProactiveFinding.finding_type == finding_type)
    stmt = stmt.order_by(ProactiveFinding.last_seen_at.desc()).limit(limit).offset(offset)
    return (await session.execute(stmt)).scalars().all()


async def list_active_findings_by_type(
    session: AsyncSession, *, organization_id: uuid.UUID, finding_type: str
) -> Sequence[ProactiveFinding]:
    """Every currently-active finding of one type, for a detection run's
    reconciliation half (finding which previously-active findings this
    run's candidates no longer support)."""
    stmt = select(ProactiveFinding).where(
        ProactiveFinding.organization_id == organization_id,
        ProactiveFinding.finding_type == finding_type,
        ProactiveFinding.status == _ACTIVE,
    )
    return (await session.execute(stmt)).scalars().all()


async def list_evidence(
    session: AsyncSession, finding_id: uuid.UUID
) -> Sequence[ProactiveFindingEvidence]:
    stmt = select(ProactiveFindingEvidence).where(ProactiveFindingEvidence.finding_id == finding_id)
    return (await session.execute(stmt)).scalars().all()


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------


async def upsert_finding(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID | None,
    finding_type: str,
    fingerprint: str,
    title: str,
    summary: str,
    support_count: int,
    detector_name: str,
    seen_at: datetime,
) -> tuple[ProactiveFinding, str]:
    """Create, update, reactivate, or leave unchanged the one finding with
    this logical identity (`organization_id` + `fingerprint`), returning
    `(row, action)` where `action` is `"created"`, `"updated"`,
    `"reactivated"`, or `"unchanged"`.

    Queried directly by logical identity rather than a database-level
    upsert, since a previously-`"inactive"` row that matches must be
    revived (an `INSERT ... ON CONFLICT DO NOTHING` would not do that) --
    the identical reasoning `core.graph.repository.upsert_derived_edge`
    gives for its own upsert shape.

    `first_seen_at` is set once, on creation, and never touched again --
    "when did this pattern first appear" must survive every later update.
    `last_seen_at` always advances to `seen_at`.
    """
    existing = await get_finding_by_fingerprint(
        session, organization_id=organization_id, fingerprint=fingerprint
    )

    if existing is None:
        row = ProactiveFinding(
            organization_id=organization_id,
            project_id=project_id,
            finding_type=finding_type,
            status=_ACTIVE,
            title=title,
            summary=summary,
            fingerprint=fingerprint,
            support_count=support_count,
            detector_name=detector_name,
            first_seen_at=seen_at,
            last_seen_at=seen_at,
        )
        session.add(row)
        await session.flush()
        return row, "created"

    was_inactive = existing.status != _ACTIVE
    unchanged = (
        not was_inactive
        and existing.title == title
        and existing.summary == summary
        and existing.support_count == support_count
    )
    if unchanged:
        return existing, "unchanged"

    existing.status = _ACTIVE
    existing.project_id = project_id
    existing.title = title
    existing.summary = summary
    existing.support_count = support_count
    existing.detector_name = detector_name
    existing.last_seen_at = seen_at
    if was_inactive:
        existing.deactivated_at = None
    await session.flush()
    return existing, ("reactivated" if was_inactive else "updated")


async def update_support(
    session: AsyncSession, finding_id: uuid.UUID, *, support_count: int, seen_at: datetime
) -> int:
    """Update an active finding's support count and `last_seen_at` in
    place, without touching its status. Used by `service.
    handle_evidence_entity_removed` when removing one stale evidence row
    still leaves enough support to keep the finding active -- as opposed to
    `deactivate_finding`, which is the below-threshold branch of the same
    recompute.
    """
    stmt = (
        update(ProactiveFinding)
        .where(ProactiveFinding.id == finding_id)
        .values(support_count=support_count, last_seen_at=seen_at)
    )
    return int((await session.execute(stmt)).rowcount or 0)


async def deactivate_finding(
    session: AsyncSession, finding_id: uuid.UUID, *, deactivated_at: datetime
) -> int:
    """Idempotent: a second call matches zero rows (already inactive)."""
    stmt = (
        update(ProactiveFinding)
        .where(ProactiveFinding.id == finding_id, ProactiveFinding.status == _ACTIVE)
        .values(status=_INACTIVE, deactivated_at=deactivated_at)
    )
    return int((await session.execute(stmt)).rowcount or 0)


async def replace_evidence(
    session: AsyncSession, finding_id: uuid.UUID, evidence: Sequence[tuple[str, uuid.UUID, str]]
) -> None:
    """Replace a finding's entire evidence set in one call: delete every
    existing row, insert exactly what `evidence` names.

    Wholesale replace rather than a diff -- simpler, and still idempotent
    (running detection twice against the same source state deletes-then-
    reinserts the identical set both times, which is a no-op in effect).
    Both statements run in the caller's existing transaction, so a finding
    and its evidence are never observably out of sync.
    """
    await session.execute(
        sql_delete(ProactiveFindingEvidence).where(
            ProactiveFindingEvidence.finding_id == finding_id
        )
    )
    for entity_type, entity_id, role in evidence:
        session.add(
            ProactiveFindingEvidence(
                finding_id=finding_id, entity_type=entity_type, entity_id=entity_id, role=role
            )
        )
    await session.flush()


async def remove_evidence_for_entity(
    session: AsyncSession, *, entity_type: str, entity_id: uuid.UUID
) -> Sequence[uuid.UUID]:
    """Delete every evidence row naming `(entity_type, entity_id)`, across
    every finding, returning the distinct `finding_id`s touched.

    Used by `service.handle_evidence_entity_removed`'s physical-cleanup
    half: once a row is deleted here, that finding's support must be
    recomputed from what remains -- this function only removes the stale
    reference, the caller decides what that means for the finding's status.
    """
    stmt = select(ProactiveFindingEvidence.finding_id).where(
        ProactiveFindingEvidence.entity_type == entity_type,
        ProactiveFindingEvidence.entity_id == entity_id,
    )
    finding_ids = (await session.execute(stmt)).scalars().all()
    if not finding_ids:
        return []
    await session.execute(
        sql_delete(ProactiveFindingEvidence).where(
            ProactiveFindingEvidence.entity_type == entity_type,
            ProactiveFindingEvidence.entity_id == entity_id,
        )
    )
    return finding_ids
