"""Persistence for core/audit -- the only module that writes `audit_logs`.

Owned by: core/audit. Pure data access: functions here take an AsyncSession,
issue exactly one statement, and return ORM rows (or None). They never commit,
never map to Pydantic, and never enforce business rules -- that is the
service's job (ARCHITECTURE.md section 3: infrastructure/persistence holds no
business logic).

Append-only by contract (DATABASE_DESIGN.md): this module exposes only INSERT
and SELECT. No update/delete function exists, so the append-only guarantee is
structural, not merely a convention someone must remember.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.schemas import AuditLogQuery
from app.database.models.core_models import AuditLog


async def insert(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None,
    event_metadata: dict | None,
) -> AuditLog:
    """Append one audit row and return it with server-side defaults populated.

    `actor` is the caller's `Identity.audit_tag` (resolved by the service, not
    here). The row is flushed and refreshed so DB-generated columns (`id` via
    gen_random_uuid(), `occurred_at` via now()) are available to the caller;
    the surrounding transaction is committed by the service / session scope,
    not by this function.
    """
    row = AuditLog(
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        event_metadata=event_metadata,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def list_entries(
    session: AsyncSession,
    query: AuditLogQuery,
) -> Sequence[AuditLog]:
    """Return audit rows matching `query`, newest first.

    Filters are optional and AND-combined; results are ordered by
    `occurred_at` descending (matching the `ix_audit_logs_occurred_at_desc`
    index) and paginated via limit/offset.
    """
    stmt = select(AuditLog)

    if query.resource_type is not None:
        stmt = stmt.where(AuditLog.resource_type == query.resource_type)
    if query.resource_id is not None:
        stmt = stmt.where(AuditLog.resource_id == query.resource_id)
    if query.actor is not None:
        stmt = stmt.where(AuditLog.actor == query.actor)

    stmt = stmt.order_by(AuditLog.occurred_at.desc()).limit(query.limit).offset(query.offset)

    result = await session.execute(stmt)
    return result.scalars().all()