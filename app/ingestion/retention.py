"""Scheduled document lifecycle: dead chunks, version tombstones, and connector-deletion cleanup.

Owned by: ingestion/. Called by the daily maintenance job (`app.ingestion.workers.tasks.scheduled_maintenance`)
and by the per-connector cleanup task (`cleanup_connector_documents_task`). Reuses `ingestion.lifecycle`
(the batched mark/purge machinery behind `scripts/backfill_superseded_documents.py`) rather than duplicating it.

THE THREE JOBS

1. **Dead chunks** (`purge_dead_chunks`): for every organization, (a) marks superseded any non-latest version that
   an earlier failure left looking "current" (the lifecycle `mark` phase), then (b) deletes the chunks and
   embeddings -- from ALL FOUR chunk tables -- of superseded documents, and of documents soft-deleted more than
   `DOCUMENT_SOFT_DELETE_CHUNK_RETENTION_DAYS` ago. Current, non-deleted documents are never selected. `documents`
   and `document_metadata` rows are not touched here, so every `document_id` still resolves.

2. **Version tombstones** (`tombstone_old_versions`): per logical document `(source, external_id)`, a superseded
   version outside the newest `DOCUMENT_VERSION_KEEP_LATEST` AND superseded more than
   `DOCUMENT_VERSION_RETENTION_DAYS` ago has its `document_metadata` rows removed and `documents.tombstoned_at`
   set. **The `documents` row is never deleted**: its id, title, source URL, content hash and version stay, so every
   citation, knowledge-gap link, audit reference or pattern-evidence reference that names the id keeps resolving
   (nothing in the schema holds the document's content except its chunks, which job 1 already removed). A
   tombstone is reversible: an A -> B -> A revert revives the row and replaces its metadata.

3. **Connector deletion** (`cleanup_connector_documents`): after a connector is disconnected, soft-deletes the
   documents it produced and removes their chunks in batches. Documents do not record their connector (the
   ingestion write path is intentionally untouched), so a document is attributed to a connector ONLY when the
   organization has exactly one connector of that `source`; with two or more of the same kind the cleanup does
   nothing and logs `connector_cleanup_skipped_ambiguous`. Never touches another connector's or another
   organization's documents. Idempotent and retryable: the daily job re-runs it for every disconnected connector.

All work is one short transaction per batch, with the organization's tenant context set (RLS confines every
statement to that organization even if a predicate were wrong).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion import lifecycle
from app.ingestion.lifecycle import CHUNK_TABLES, SessionScope
from app.shared.config.logging import get_logger
from app.shared.config.settings import Settings, get_settings

logger = get_logger(__name__)

MAX_BATCHES = 5000


@dataclass
class DocumentCleanupReport:
    versions_marked: int = 0
    chunks_purged: dict[str, int] = field(default_factory=dict)
    versions_tombstoned: int = 0
    tombstones_healed: int = 0
    connectors_cleaned: int = 0
    connector_documents_deleted: int = 0
    connector_chunks_purged: int = 0
    errors: list[str] = field(default_factory=list)


# ------------------------------------------------------------------------------------- 1. dead chunks


async def purge_dead_chunks(
    session_scope: SessionScope,
    organization_ids: list[uuid.UUID],
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    sleep_seconds: float | None = None,
) -> tuple[int, dict[str, int]]:
    """Mark missed supersessions, then purge dead chunks, for each organization (failures isolated)."""
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=settings.document_soft_delete_chunk_retention_days)
    marked = 0
    purged: dict[str, int] = {t: 0 for t in CHUNK_TABLES}
    for organization_id in organization_ids:
        try:
            report = await lifecycle.run_backfill(
                session_scope,
                organization_ids=[organization_id],
                apply=not settings.retention_dry_run,
                phase="all",
                batch_size=settings.retention_batch_size,
                sleep_seconds=settings.retention_batch_pause_seconds if sleep_seconds is None else sleep_seconds,
                include_soft_deleted=False,
                soft_deleted_before=cutoff,
                log=lambda message: logger.debug("document_chunk_cleanup", detail=message),
            )
        except Exception:  # noqa: BLE001 - isolation: one organization's failure never stops the rest
            logger.exception("document_chunk_cleanup_failed", organization_id=str(organization_id))
            continue
        for org in report.organizations:
            marked += org.versions_marked
            source = org.chunks_to_purge if settings.retention_dry_run else org.chunks_purged
            for table, count in source.items():
                purged[table] = purged.get(table, 0) + count
    return marked, purged


# ------------------------------------------------------------------------------------- 2. tombstones

_CANDIDATES = """
    WITH ranked AS (
        SELECT id, superseded_at, tombstoned_at,
               ROW_NUMBER() OVER (PARTITION BY source, external_id
                                  ORDER BY version DESC, created_at DESC, id DESC) AS rn
        FROM documents
        WHERE organization_id = :org AND source <> 'manual'
    )
    SELECT id FROM ranked
    WHERE rn > :keep AND superseded_at IS NOT NULL AND superseded_at < :before AND tombstoned_at IS NULL
    LIMIT :batch
"""


async def tombstone_batch(
    session: AsyncSession, organization_id: uuid.UUID, *, keep_latest: int, superseded_before: datetime, batch_size: int
) -> int:
    """Tombstone up to `batch_size` eligible old versions; returns how many (0 = nothing left)."""
    ids = (
        await session.execute(
            text(_CANDIDATES),
            {"org": organization_id, "keep": keep_latest, "before": superseded_before, "batch": batch_size},
        )
    ).scalars().all()
    if not ids:
        return 0
    await session.execute(text("DELETE FROM document_metadata WHERE document_id = ANY(:ids)"), {"ids": list(ids)})
    await session.execute(
        text("UPDATE documents SET tombstoned_at = now(), updated_at = updated_at WHERE id = ANY(:ids)"),
        {"ids": list(ids)},
    )
    return len(ids)


async def count_tombstone_candidates(
    session: AsyncSession, organization_id: uuid.UUID, *, keep_latest: int, superseded_before: datetime
) -> int:
    return len(
        (
            await session.execute(
                text(_CANDIDATES.replace("LIMIT :batch", "")),
                {"org": organization_id, "keep": keep_latest, "before": superseded_before},
            )
        ).scalars().all()
    )


async def tombstone_old_versions(
    session_scope: SessionScope,
    organization_ids: list[uuid.UUID],
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    before = now - timedelta(days=settings.document_version_retention_days)
    total = 0
    for organization_id in organization_ids:
        try:
            if settings.retention_dry_run:
                async with session_scope() as session:
                    await lifecycle.begin_org_batch(session, organization_id)
                    total += await count_tombstone_candidates(
                        session, organization_id, keep_latest=settings.document_version_keep_latest,
                        superseded_before=before,
                    )
                continue
            for _ in range(MAX_BATCHES):
                async with session_scope() as session:
                    await lifecycle.begin_org_batch(session, organization_id)
                    n = await tombstone_batch(
                        session, organization_id, keep_latest=settings.document_version_keep_latest,
                        superseded_before=before, batch_size=settings.retention_batch_size,
                    )
                total += n
                if n == 0:
                    break
                await sleep(settings.retention_batch_pause_seconds)
        except Exception:  # noqa: BLE001
            logger.exception("document_tombstone_failed", organization_id=str(organization_id))
    return total


async def heal_revived_tombstones(
    session_scope: SessionScope,
    organization_ids: list[uuid.UUID],
    *,
    settings: Settings | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """A tombstoned version that became current again (an A -> B -> A revert revives the row and replaces its
    metadata) is no longer a tombstone. Ingestion's own code is not modified to clear the flag, so the daily job
    does it: `tombstoned_at IS NOT NULL AND superseded_at IS NULL` is never a valid state."""
    settings = settings or get_settings()
    healed = 0
    if settings.retention_dry_run:
        return 0
    for organization_id in organization_ids:
        try:
            for _ in range(MAX_BATCHES):
                async with session_scope() as session:
                    await lifecycle.begin_org_batch(session, organization_id)
                    result = await session.execute(
                        text(
                            "UPDATE documents SET tombstoned_at = NULL, updated_at = updated_at WHERE id IN ("
                            "SELECT id FROM documents WHERE organization_id = :o AND tombstoned_at IS NOT NULL "
                            "AND superseded_at IS NULL LIMIT :n)"
                        ),
                        {"o": organization_id, "n": settings.retention_batch_size},
                    )
                    n = int(result.rowcount or 0)
                healed += n
                if n < settings.retention_batch_size:
                    break
                await sleep(settings.retention_batch_pause_seconds)
        except Exception:  # noqa: BLE001
            logger.exception("document_tombstone_heal_failed", organization_id=str(organization_id))
    return healed


# ------------------------------------------------------------------------------------- 3. connector deletion


async def _attribution_clause(
    session: AsyncSession, organization_id: uuid.UUID, connector_config_id: uuid.UUID
) -> tuple[str, dict] | None:
    """SQL fragment selecting exactly the documents that belong to this connector, or None when nothing may be
    deleted for it: the connector is unknown, not disconnected, or its documents cannot be told apart from
    another connector's.

    Documents do not record which connector produced them (ingestion's write path is deliberately unchanged), so
    the only attribution that is NOT a guess is: the organization has exactly ONE connector of that `source`
    (any status). Then every document of that source in the organization is that connector's. With two or more
    connectors of the same source the documents are indistinguishable and the cleanup does nothing (logged),
    rather than risk deleting another connector's data."""
    row = (
        await session.execute(
            text("SELECT source, status FROM connector_configs WHERE id = :c AND organization_id = :o"),
            {"c": connector_config_id, "o": organization_id},
        )
    ).first()
    if row is None or row.status != "disconnected":
        return None
    same_source = int(
        (
            await session.execute(
                text("SELECT count(*) FROM connector_configs WHERE organization_id = :o AND source = :s"),
                {"o": organization_id, "s": row.source},
            )
        ).scalar_one()
    )
    if same_source != 1:
        logger.warning(
            "connector_cleanup_skipped_ambiguous",
            organization_id=str(organization_id),
            connector_config_id=str(connector_config_id),
            connectors_with_same_source=same_source,
        )
        return None
    return "(d.source = :src)", {"src": row.source}


async def cleanup_connector_documents(
    session_scope: SessionScope,
    organization_id: uuid.UUID,
    connector_config_id: uuid.UUID,
    *,
    batch_size: int = 1000,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    pause_seconds: float = 0.1,
) -> tuple[int, int] | None:
    """Soft-delete the connector's documents and remove their chunks. Returns (documents_deleted,
    chunks_deleted), or None if the connector is not a disconnected connector of this organization."""
    async with session_scope() as session:
        await lifecycle.begin_org_batch(session, organization_id)
        attribution = await _attribution_clause(session, organization_id, connector_config_id)
    if attribution is None:
        return None
    clause, params = attribution
    documents = 0
    for _ in range(MAX_BATCHES):
        async with session_scope() as session:
            await lifecycle.begin_org_batch(session, organization_id)
            result = await session.execute(
                text(
                    f"""
                    UPDATE documents d SET deleted_at = now(), updated_at = d.updated_at
                    WHERE d.id IN (
                        SELECT d.id FROM documents d
                        WHERE d.organization_id = :org AND d.deleted_at IS NULL AND {clause}
                        LIMIT :batch
                    )
                    """  # noqa: S608 - the clause is built from fixed fragments above
                ),
                {"org": organization_id, "batch": batch_size, **params},
            )
            n = int(result.rowcount or 0)
        documents += n
        if n < batch_size:
            break
        await sleep(pause_seconds)

    chunks = 0
    for table in CHUNK_TABLES:
        for _ in range(MAX_BATCHES):
            async with session_scope() as session:
                await lifecycle.begin_org_batch(session, organization_id)
                result = await session.execute(
                    text(
                        f"""
                        DELETE FROM {table} WHERE id IN (
                            SELECT c.id FROM {table} c JOIN documents d ON d.id = c.document_id
                            WHERE d.organization_id = :org AND c.organization_id = :org
                              AND d.deleted_at IS NOT NULL AND {clause}
                            LIMIT :batch
                        )
                        """  # noqa: S608
                    ),
                    {"org": organization_id, "batch": batch_size, **params},
                )
                n = int(result.rowcount or 0)
            chunks += n
            if n < batch_size:
                break
            await sleep(pause_seconds)
    logger.info(
        "connector_documents_cleaned",
        organization_id=str(organization_id),
        connector_config_id=str(connector_config_id),
        documents_deleted=documents,
        chunks_deleted=chunks,
    )
    return documents, chunks


async def sweep_disconnected_connectors(
    session_scope: SessionScope,
    organization_ids: list[uuid.UUID],
    *,
    settings: Settings | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[int, int, int, list[str]]:
    """Daily safety net for the queued cleanup: re-run it for every disconnected connector (idempotent; cheap when
    nothing is left). Returns (connectors, documents_deleted, chunks_deleted, errors)."""
    settings = settings or get_settings()
    connectors = documents = chunks = 0
    errors: list[str] = []
    if settings.retention_dry_run:
        return 0, 0, 0, errors
    for organization_id in organization_ids:
        try:
            async with session_scope() as session:
                await lifecycle.begin_org_batch(session, organization_id)
                ids = list(
                    (
                        await session.execute(
                            text("SELECT id FROM connector_configs WHERE organization_id = :o AND status = 'disconnected'"),
                            {"o": organization_id},
                        )
                    ).scalars().all()
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{organization_id}: {type(exc).__name__}")
            continue
        for connector_id in ids:
            try:
                outcome = await cleanup_connector_documents(
                    session_scope, organization_id, connector_id, batch_size=settings.retention_batch_size,
                    sleep=sleep, pause_seconds=settings.retention_batch_pause_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("connector_cleanup_failed", connector_config_id=str(connector_id))
                errors.append(f"{connector_id}: {type(exc).__name__}")
                continue
            if outcome is not None:
                connectors += 1
                documents += outcome[0]
                chunks += outcome[1]
    return connectors, documents, chunks, errors


# ------------------------------------------------------------------------------------- orchestration


async def run_document_cleanup(
    session_scope: SessionScope,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> DocumentCleanupReport:
    """The three jobs, in order, each isolated from the others' failures."""
    settings = settings or get_settings()
    report = DocumentCleanupReport()
    async with session_scope() as session:
        organization_ids = [
            row for row in (await session.execute(text("SELECT id FROM organizations ORDER BY id"))).scalars().all()
        ]
    try:
        report.versions_marked, report.chunks_purged = await purge_dead_chunks(
            session_scope, organization_ids, settings=settings, now=now
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("document_chunk_cleanup_failed")
        report.errors.append(f"chunks: {type(exc).__name__}")
    try:
        report.tombstones_healed = await heal_revived_tombstones(session_scope, organization_ids, settings=settings, sleep=sleep)
    except Exception as exc:  # noqa: BLE001
        logger.exception("document_tombstone_heal_failed")
        report.errors.append(f"tombstone_heal: {type(exc).__name__}")
    try:
        report.versions_tombstoned = await tombstone_old_versions(
            session_scope, organization_ids, settings=settings, now=now, sleep=sleep
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("document_tombstone_failed")
        report.errors.append(f"tombstones: {type(exc).__name__}")
    try:
        connectors, docs, chunks, errors = await sweep_disconnected_connectors(
            session_scope, organization_ids, settings=settings, sleep=sleep
        )
        report.connectors_cleaned, report.connector_documents_deleted, report.connector_chunks_purged = (
            connectors, docs, chunks,
        )
        report.errors.extend(errors)
    except Exception as exc:  # noqa: BLE001
        logger.exception("connector_sweep_failed")
        report.errors.append(f"connectors: {type(exc).__name__}")
    logger.info(
        "document_cleanup_completed",
        versions_marked=report.versions_marked,
        chunks_purged=report.chunks_purged,
        versions_tombstoned=report.versions_tombstoned,
        connectors_cleaned=report.connectors_cleaned,
        connector_documents_deleted=report.connector_documents_deleted,
        errors=len(report.errors),
        dry_run=settings.retention_dry_run,
    )
    return report
