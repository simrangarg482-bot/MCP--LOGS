"""Public interface for core/audit.

Owned by: core/audit. This is the module's contract (API_DESIGN.md section 2:
`record_audit_event`). Business rules and ORM->Pydantic mapping live here;
raw SQL lives in repository.py; the wire/HTTP concerns live in the future
api/ and mcp/ layers.

Transaction model: the session is a parameter, not created here. Callers pass
the session that is already running their domain mutation, so the audit row
and the change it describes are one atomic unit -- if the domain write rolls
back, so does its audit entry, and vice versa. This is why services take a
session rather than opening their own via session_scope().
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import repository
from app.core.audit.schemas import AuditLogEntry, AuditLogQuery
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity

logger = get_logger(__name__)


async def record_audit_event(
    session: AsyncSession,
    actor: Identity,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None = None,
    metadata: dict | None = None,
) -> None:
    """Append one entry to the append-only audit trail.

    Called by every mutating core/ operation (API_DESIGN.md section 2). The
    `actor` string persisted is `Identity.audit_tag` -- the single source of
    the `user:<id>` / `agent:<name>` format -- so the recorded actor always
    matches the identity that performed the action.

    Returns None: the audit trail is a side effect of a domain operation, not
    a resource the caller reads back inline. Use `query_audit_log` to read.
    """
    await repository.insert(
        session,
        actor=actor.audit_tag,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        event_metadata=metadata,
    )
    # Structured breadcrumb; the durable record is the row itself.
    logger.info(
        "audit_event_recorded",
        actor=actor.audit_tag,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
    )


async def query_audit_log(
    session: AsyncSession,
    query: AuditLogQuery,
) -> list[AuditLogEntry]:
    """Read the audit trail, newest first, filtered/paginated per `query`.

    Maps ORM rows into `AuditLogEntry` so the transactional ORM objects never
    cross the module boundary (ARCHITECTURE.md section 2).
    """
    rows = await repository.list_entries(session, query)
    return [AuditLogEntry.model_validate(row) for row in rows]