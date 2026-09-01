"""Persistence for ingestion/ -- `ingestion_jobs`, `documents`,
`document_metadata` -- plus a read-only view onto core/tenancy's
`connector_configs`.

Owned by: ingestion/. Pure data access, same discipline as every other
repository.py in this codebase: one statement per function, ORM rows in/out,
no business rules, no ORM->Pydantic mapping.

Reads `app.database.models.tenancy_models.ConnectorConfig` directly rather
than going through `core.tenancy`'s service layer -- a deliberate,
user-confirmed choice (see `app/ingestion/schemas.py`'s
`ResolvedConnectorConfig` docstring): PROJECT_PLAN.md section 9.8 lists
ingestion's dependencies as retrieval/database/shared only, so reading via
`database/` directly, rather than adding an undocumented ingestion -> core
dependency, keeps that list accurate. This blurs DATABASE_DESIGN.md's "read
only through the owning module's interface" convention slightly for reads;
writes back to `connector_configs` still go through
`core.tenancy.service.update_connector_sync_status` (called from
`ingestion/service.py`, not here), respecting write ownership.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.ingestion_models import Document, DocumentMetadata, IngestionJob
from app.database.models.tenancy_models import ConnectorConfig
from app.ingestion.schemas import DocumentMetadataEntry

# --- Connector configs (read-only; see module docstring) --------------------


async def get_connector_config(
    session: AsyncSession, connector_config_id: uuid.UUID
) -> ConnectorConfig | None:
    """Fetch one `connector_configs` row by primary key, or None if absent.

    Milestone 10 RLS note: `connector_configs` is RLS-protected -- this call
    only returns the row if `app.database.session.set_tenant_context` has
    already been set to match its `organization_id` on this session. Every
    caller that doesn't already know that organization_id ahead of time (see
    `resolve_connector_config_organization_id` below) must resolve it first.
    """
    return await session.get(ConnectorConfig, connector_config_id)


async def resolve_connector_config_organization_id(
    session: AsyncSession, connector_config_id: uuid.UUID
) -> uuid.UUID | None:
    """Resolve just the `organization_id` a `connector_configs` row belongs
    to, bypassing RLS via the narrow `resolve_connector_config_organization`
    SQL function (see the migration that defines it,
    `d2e5f8a3c1b6_milestone_10_rls_bypass_functions.py`, for the full
    reasoning).

    The one legitimate reason to call this: a worker (`ingestion.service.
    _execute_ingestion_job`) has only a bare `connector_config_id` and no
    `Identity`/org context yet to `set_tenant_context` with -- this function
    exists solely to break that chicken-and-egg deadlock, returning nothing
    but the org id. The caller is expected to call `set_tenant_context` with
    the result, then call `get_connector_config` above for the actual row --
    never to treat this function's result as a substitute for that.

    Returns None if no such row exists (mirrors `get_connector_config`'s own
    `None`-for-missing convention).
    """
    result = await session.execute(
        text("SELECT resolve_connector_config_organization(:config_id)"),
        {"config_id": connector_config_id},
    )
    return result.scalar_one_or_none()


async def list_active_connector_config_ids(session: AsyncSession) -> Sequence[uuid.UUID]:
    """Return the ids of every connector configuration eligible for the
    periodic reconciliation pass (PROJECT_PLAN.md section 4.4: "a periodic
    reconciliation pass even for webhook-supported sources, to catch
    anything a missed/failed webhook delivery would otherwise silently
    drop").

    Deliberately cross-tenant/unscoped -- no `organization_id` filter, no
    `actor`, and (Milestone 10) no `set_tenant_context` call either: this is
    the one legitimate exception to "every ingestion read is org-scoped,"
    the same way `core.tenancy.repository.list_organizations` is for the
    Knowledge Gap Agent's scan. Goes through the narrow
    `list_active_connector_config_ids` SQL function (see
    `d2e5f8a3c1b6_milestone_10_rls_bypass_functions.py`) to bypass RLS for
    exactly this enumeration, returning only ids -- never full rows -- since
    the sole caller (`ingestion.workers.tasks.scheduled_reconciliation`)
    only ever needs an id to enqueue a job with. Each *job* this produces
    still only touches its own connector_config's organization -- tenant
    isolation is enforced at the work itself (`_execute_ingestion_job`
    resolves and sets that one connector_config's org before reading
    anything else), not at this enumeration step.

    Includes `"error"` alongside `"active"`: a connector that failed its
    last sync should get picked up and retried on the next reconciliation
    pass, not silently excluded until an admin manually intervenes.
    Excludes `"connecting"` (onboarding not yet finished) and
    `"disconnected"` (deliberately turned off) -- both filtered inside the
    SQL function itself.
    """
    result = await session.execute(text("SELECT id FROM list_active_connector_config_ids()"))
    return result.scalars().all()


async def get_connector_config_for_source(
    session: AsyncSession, organization_id: uuid.UUID, source: str
) -> ConnectorConfig | None:
    """Fetch the most recently created connector configuration registered
    for `organization_id` + `source`. Backs `reindex(document_id)`: given a
    document, find *some* connector_config that can re-sync its source.

    Doesn't attempt to disambiguate further if an organization has
    registered more than one connector_config for the same source (e.g. two
    separate GitHub app installations) -- an edge case out of scope here.
    """
    stmt = (
        select(ConnectorConfig)
        .where(ConnectorConfig.organization_id == organization_id, ConnectorConfig.source == source)
        .order_by(ConnectorConfig.created_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# --- Ingestion jobs ----------------------------------------------------------


async def insert_ingestion_job(
    session: AsyncSession, *, organization_id: uuid.UUID, connector_config_id: uuid.UUID
) -> IngestionJob:
    """Recover an interrupted predecessor, then create a queued job row.

    The connector-level distributed lock guarantees that production callers
    reach this function one at a time. If the previous worker process died
    after Redis or Postgres disappeared, its row can otherwise remain
    ``running`` forever. A new, lock-owning attempt is authoritative evidence
    that no predecessor is still healthy, so close those stale rows before
    creating the replacement. This is deliberately in the same transaction
    as the insert: observers never see the predecessor recovered without its
    replacement attempt also existing.
    """
    await session.execute(
        update(IngestionJob)
        .where(
            IngestionJob.organization_id == organization_id,
            IngestionJob.connector_config_id == connector_config_id,
            IngestionJob.status == "running",
        )
        .values(
            status="failed",
            failed_stage=func.coalesce(IngestionJob.failed_stage, "worker_interrupted"),
            last_error_type="WorkerInterrupted",
            completed_at=func.now(),
        )
    )
    row = IngestionJob(
        organization_id=organization_id,
        connector_config_id=connector_config_id,
        status="queued",
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_ingestion_job_by_id(session: AsyncSession, job_id: uuid.UUID) -> IngestionJob | None:
    """Fetch a single ingestion job by primary key, or None if absent."""
    return await session.get(IngestionJob, job_id)


async def update_ingestion_job(
    session: AsyncSession, job_id: uuid.UUID, **fields: Any
) -> IngestionJob | None:
    """Apply `fields` to an ingestion job row, returning the updated row or
    None if it doesn't exist. Generic, dict-driven updater -- same rationale
    as `core.incidents.repository.update_incident`: a job has enough
    independently-updatable fields (status, failed_stage,
    documents_processed, started_at, completed_at) that a narrow function
    per field would multiply faster than it's worth.
    """
    row = await session.get(IngestionJob, job_id)
    if row is None:
        return None
    for key, value in fields.items():
        setattr(row, key, value)
    await session.flush()
    await session.refresh(row)
    return row


# --- Documents -----------------------------------------------------------------


async def get_latest_document(
    session: AsyncSession, organization_id: uuid.UUID, source: str, external_id: str
) -> Document | None:
    """Fetch the highest-`version` row for (organization_id, source,
    external_id), or None if this item has never been ingested before.

    Changed content becomes a *new* row with `version` incremented rather
    than an update to the existing one (DATABASE_DESIGN.md: "gets a new row
    with version incremented ... rather than overwriting history"), so
    "the current version" means "the row with the highest version number
    for this key," not "the only row for this key."
    """
    stmt = (
        select(Document)
        .where(
            Document.organization_id == organization_id,
            Document.source == source,
            Document.external_id == external_id,
        )
        .order_by(Document.version.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def insert_document(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    source: str,
    external_id: str,
    content_hash: str,
    title: str | None,
    source_url: str | None,
    version: int,
    acl_permission_code: str | None = None,
) -> Document:
    """Insert one document row at `version` and return it. PostgreSQL's
    implicit ``RETURNING`` populates generated defaults during ``flush``;
    a separate ``refresh`` would add an unnecessary database round trip for
    every ingested document. Always `status="proposed"` -- publishing is a
    separate, not-yet-built review step (ARCHITECTURE.md section 5's
    human-review gate), matching `documents.status`'s documented lifecycle.

    `acl_permission_code` defaults to `None` (no ACL restriction): no caller
    in this codebase currently passes a non-null value -- see
    ENGINEERING_DECISIONS.md #007's flagged gap. The parameter exists so a
    future feature (e.g. connector-config-level tagging) has somewhere to
    plug in without another signature change.
    """
    row = Document(
        organization_id=organization_id,
        project_id=project_id,
        source=source,
        external_id=external_id,
        content_hash=content_hash,
        title=title,
        source_url=source_url,
        status="proposed",
        version=version,
        acl_permission_code=acl_permission_code,
    )
    session.add(row)
    await session.flush()
    return row


async def get_document_by_id(session: AsyncSession, document_id: uuid.UUID) -> Document | None:
    """Fetch a single document by primary key, or None if absent.

    Milestone 10 RLS note: `documents` is RLS-protected -- same caveat as
    `get_connector_config` above. `ingestion.service.reindex`'s first read
    is a bare-PK lookup with no org context yet; see
    `resolve_document_organization_id` below for how it resolves one first.
    """
    return await session.get(Document, document_id)


async def resolve_document_organization_id(
    session: AsyncSession, document_id: uuid.UUID
) -> uuid.UUID | None:
    """Resolve just the `organization_id` a `documents` row belongs to,
    bypassing RLS via the narrow `resolve_document_organization` SQL
    function -- the same pattern, and the same reasoning, as
    `resolve_connector_config_organization_id` above; see that function's
    docstring and `d2e5f8a3c1b6_milestone_10_rls_bypass_functions.py`.

    The one legitimate caller: `ingestion.service.reindex`, which starts
    from a bare `document_id` with no org context yet to
    `set_tenant_context` with.
    """
    result = await session.execute(
        text("SELECT resolve_document_organization(:document_id)"),
        {"document_id": document_id},
    )
    return result.scalar_one_or_none()


# --- Document metadata -------------------------------------------------------


async def insert_document_metadata(
    session: AsyncSession, *, document_id: uuid.UUID, entries: list[DocumentMetadataEntry]
) -> None:
    """Bulk-insert metadata rows for `document_id`.

    Never needs "replace" semantics: each content change gets a brand-new
    `Document` row (see `get_latest_document`'s docstring), so a new
    document_id always starts with zero metadata rows -- there is nothing
    stale to delete first.
    """
    for entry in entries:
        session.add(DocumentMetadata(document_id=document_id, key=entry.key, value=entry.value))
    await session.flush()


async def list_document_metadata(
    session: AsyncSession, document_id: uuid.UUID
) -> Sequence[DocumentMetadata]:
    """Return every metadata row for `document_id`."""
    stmt = select(DocumentMetadata).where(DocumentMetadata.document_id == document_id)
    result = await session.execute(stmt)
    return result.scalars().all()
