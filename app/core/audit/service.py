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

Milestone 3 (PROJECT_PLAN.md section 9.4 / "Milestone 3 -- Incident
Management") calls for extending this module to carry `organization_id`,
"largely additive to what already exists": `AuditLog.organization_id` was
already present on the ORM model (nullable -- see its docstring), but nothing
in this application layer populated or filtered on it. `record_audit_event`'s
signature is unchanged -- `organization_id` is derived from `actor.organization_id`
internally, so none of its existing callers (core/tenancy, core/incidents)
needed to change. `query_audit_log` did change: it previously took no actor
and no organization scoping at all, meaning any caller could read every
organization's audit trail. That gap had gone unnoticed only because nothing
calls it yet (no API/MCP layer exists); it is closed here before a caller
ever gets the chance to rely on the unscoped behavior.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import repository
from app.core.audit.schemas import AuditLogEntry, AuditLogQuery
from app.core.exceptions import PermissionDeniedError
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity

logger = get_logger(__name__)


def _ensure_same_organization(actor: Identity, organization_id: uuid.UUID) -> None:
    """Tenant-isolation guard -- identical in spirit to the copies in
    `core.tenancy.service` and `core.incidents.service`. This is now the
    third occurrence of this exact function; per the note left in
    `core.incidents.service`'s module docstring, a third occurrence is the
    point at which extracting it into a shared helper becomes worth doing.
    Not done in this change -- that would mean also editing
    `core.tenancy.service` and `core.incidents.service`, which is outside
    this task's scope (extending core/audit) -- but flagged here rather than
    silently duplicated a third time without comment.
    """
    if actor.organization_id != organization_id:
        logger.warning(
            "audit_cross_organization_denied",
            actor=actor.audit_tag,
            actor_organization_id=str(actor.organization_id),
            requested_organization_id=str(organization_id),
        )
        raise PermissionDeniedError(
            "Cannot access another organization's data.",
            error_code="audit.cross_organization_denied",
            detail={"organization_id": str(organization_id)},
        )


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
    matches the identity that performed the action. The row's
    `organization_id` is `actor.organization_id` -- every `Identity` in this
    codebase carries one, so this is unconditional, not an `if` check.

    Returns None: the audit trail is a side effect of a domain operation, not
    a resource the caller reads back inline. Use `query_audit_log` to read.
    """
    await repository.insert(
        session,
        organization_id=actor.organization_id,
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
        organization_id=str(actor.organization_id),
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
    )


async def query_audit_log(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    query: AuditLogQuery,
) -> list[AuditLogEntry]:
    """Read `organization_id`'s audit trail, newest first, filtered/paginated
    per `query`.

    Requires `actor` and `organization_id` (new in this change -- see this
    module's docstring) so a cross-organization read is denied the same way
    every other org-scoped read in this codebase is denied, rather than being
    an oversight waiting for its first caller to exploit it.

    Maps ORM rows into `AuditLogEntry` so the transactional ORM objects never
    cross the module boundary (ARCHITECTURE.md section 2).
    """
    _ensure_same_organization(actor, organization_id)
    rows = await repository.list_entries(session, organization_id, query)
    return [AuditLogEntry.model_validate(row) for row in rows]