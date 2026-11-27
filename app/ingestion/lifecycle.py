"""Backfill for the document version lifecycle (`documents.superseded_at`).

Owned by: ingestion/. Used by `scripts/backfill_superseded_documents.py`;
nothing in the request or worker path imports this module.

Why it exists: rows written before `superseded_at` existed are all
`superseded_at IS NULL`, i.e. every historical version of a re-synced document
looks "current" -- so all of them stay searchable, listed in `/knowledge`, and
keep their chunks. Ingestion heals a document the next time it touches it
(`repository.supersede_other_versions`), but documents that never change again
would stay stale forever. This module repairs them in two independent phases:

* **mark**  -- set `superseded_at` on every non-latest version of each
  `(source, external_id)` (connector documents only; `source='manual'`
  documents bump their version in place and are never superseded). Metadata
  only, no deletes. Correctness-critical and fast: as soon as rows are marked,
  every retrieval query and the browse list stop returning them.
* **purge** -- delete the derived chunks (and embeddings) of superseded
  documents from all four chunk tables. Storage-oriented, throttled, and the
  only destructive step. `documents` and `document_metadata` rows are NEVER
  deleted here, so every old `document_id` (and any citation naming it) keeps
  resolving.

Safety properties:

* Tenant-scoped: every batch runs in its own short transaction with
  `set_tenant_context` for exactly one organization and also filters
  `organization_id` explicitly. Under FORCE ROW LEVEL SECURITY a batch can only
  ever see that organization's rows. Nothing here bypasses RLS.
* Idempotent and resumable: the work queue IS the predicate
  (`superseded_at IS NULL` rows that are not the latest; chunks whose document
  is superseded). A re-run after a crash or Ctrl-C simply finds less to do.
* Bounded: small batches, one commit per batch, `lock_timeout` so a batch
  fails fast instead of queuing behind a long lock.
* Safe beside live ingestion: it only touches NON-latest versions, which live
  ingestion never writes (it supersedes the latest and inserts a new one).
  Both sides are convergent if they race on a key.
* Dry run by default at the script level; this module's `run_backfill` only
  writes when `apply=True`.

Soft-deleted documents (`deleted_at IS NOT NULL`, e.g. the vendored-file prune)
keep their chunks unless `include_soft_deleted=True`: those chunks are already
hidden from retrieval, and purging them is a separate, explicit choice.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import set_tenant_context
from app.shared.config.logging import get_logger

logger = get_logger(__name__)

# Fixed whitelist -- table names are interpolated into SQL below and must never
# come from user input.
CHUNK_TABLES: tuple[str, ...] = (
    "documentation_chunks",
    "code_chunks",
    "conversations_chunks",
    "incidents_chunks",
)

SessionScope = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass
class OrgReport:
    organization_id: uuid.UUID
    versions_to_mark: int = 0
    chunks_to_purge: dict[str, int] = field(default_factory=dict)
    versions_marked: int = 0
    chunks_purged: dict[str, int] = field(default_factory=dict)
    keys_still_with_multiple_current: int | None = None


@dataclass
class BackfillReport:
    applied: bool
    organizations: list[OrgReport] = field(default_factory=list)


# --- low-level, single-statement helpers (each takes an already tenant-scoped session) ---


async def list_organization_ids(session: AsyncSession) -> list[uuid.UUID]:
    """Every organization id. `organizations` has no RLS policy (it is the root
    of the tenancy tree), so this works without tenant context."""
    result = await session.execute(text("SELECT id FROM organizations ORDER BY id"))
    return list(result.scalars().all())


async def begin_org_batch(session: AsyncSession, organization_id: uuid.UUID) -> None:
    """Scope the CURRENT transaction to one organization and bound lock waits."""
    await set_tenant_context(session, organization_id)
    await session.execute(text("SET LOCAL lock_timeout = '5s'"))


async def count_versions_to_mark(session: AsyncSession, organization_id: uuid.UUID) -> int:
    result = await session.execute(
        text(
            """
            SELECT COALESCE(SUM(c - 1), 0) FROM (
                SELECT count(*) AS c FROM documents
                WHERE organization_id = :org AND source <> 'manual' AND superseded_at IS NULL
                GROUP BY source, external_id HAVING count(*) > 1
            ) t
            """
        ),
        {"org": organization_id},
    )
    return int(result.scalar_one())


def _purge_predicate(include_soft_deleted: bool, soft_deleted_before: object | None = None) -> str:
    """Which documents' chunks are dead. `soft_deleted_before` (a timestamp bound as `:sd_before`) purges the
    chunks of documents soft-deleted BEFORE that moment only -- the grace period used by the scheduled job."""
    if include_soft_deleted:
        return "(d.superseded_at IS NOT NULL OR d.deleted_at IS NOT NULL)"
    if soft_deleted_before is not None:
        return "(d.superseded_at IS NOT NULL OR (d.deleted_at IS NOT NULL AND d.deleted_at < :sd_before))"
    return "d.superseded_at IS NOT NULL"


async def count_chunks_to_purge(
    session: AsyncSession,
    organization_id: uuid.UUID,
    table: str,
    *,
    include_soft_deleted: bool,
    soft_deleted_before: object | None = None,
) -> int:
    assert table in CHUNK_TABLES
    params: dict[str, object] = {"org": organization_id}
    if soft_deleted_before is not None:
        params["sd_before"] = soft_deleted_before
    result = await session.execute(
        text(
            f"""
            SELECT count(*) FROM {table} c JOIN documents d ON d.id = c.document_id
            WHERE d.organization_id = :org AND c.organization_id = :org
              AND {_purge_predicate(include_soft_deleted, soft_deleted_before)}
            """
        ),
        params,
    )
    return int(result.scalar_one())


async def keys_needing_mark(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    after: tuple[str, str] | None,
    limit: int,
) -> list[tuple[str, str]]:
    """Next `limit` `(source, external_id)` keys (keyset-paginated) that still
    have more than one not-yet-superseded row."""
    cursor = "AND (source, external_id) > (:after_source, :after_external_id)" if after else ""
    params: dict[str, object] = {"org": organization_id, "limit": limit}
    if after:
        params["after_source"], params["after_external_id"] = after
    result = await session.execute(
        text(
            f"""
            SELECT source, external_id FROM documents
            WHERE organization_id = :org AND source <> 'manual' AND superseded_at IS NULL
              {cursor}
            GROUP BY source, external_id HAVING count(*) > 1
            ORDER BY source, external_id LIMIT :limit
            """
        ),
        params,
    )
    return [(row.source, row.external_id) for row in result.all()]


async def mark_key_superseded(
    session: AsyncSession, organization_id: uuid.UUID, source: str, external_id: str
) -> int:
    """Mark every non-latest, not-yet-superseded version of ONE logical
    document as superseded; returns how many rows were marked.

    "Latest" = highest `version` (ties: newest `created_at`, then id), exactly
    the row `ingestion.repository.get_latest_document` treats as current.
    `superseded_at` is set to the NEXT-NEWER version's `created_at` -- when the
    replacement actually happened -- not to now. `updated_at` is preserved.
    """
    result = await session.execute(
        text(
            """
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER w AS rn,
                       LAG(created_at) OVER w AS successor_created_at
                FROM documents
                WHERE organization_id = :org AND source = :source
                  AND external_id = :external_id AND superseded_at IS NULL
                WINDOW w AS (ORDER BY version DESC, created_at DESC, id DESC)
            )
            UPDATE documents d
               SET superseded_at = COALESCE(r.successor_created_at, now()),
                   updated_at = d.updated_at
              FROM ranked r
             WHERE d.id = r.id AND r.rn > 1
            RETURNING d.id
            """
        ),
        {"org": organization_id, "source": source, "external_id": external_id},
    )
    return len(result.all())


async def purge_chunk_batch(
    session: AsyncSession,
    organization_id: uuid.UUID,
    table: str,
    *,
    batch_size: int,
    include_soft_deleted: bool,
    soft_deleted_before: object | None = None,
) -> int:
    """Delete up to `batch_size` chunk rows of superseded documents from one
    chunk table; returns how many were deleted (0 = nothing left)."""
    assert table in CHUNK_TABLES
    params: dict[str, object] = {"org": organization_id, "limit": batch_size}
    if soft_deleted_before is not None:
        params["sd_before"] = soft_deleted_before
    result = await session.execute(
        text(
            f"""
            DELETE FROM {table} WHERE id IN (
                SELECT c.id FROM {table} c JOIN documents d ON d.id = c.document_id
                WHERE d.organization_id = :org AND c.organization_id = :org
                  AND {_purge_predicate(include_soft_deleted, soft_deleted_before)}
                LIMIT :limit
            )
            """
        ),
        params,
    )
    return int(result.rowcount or 0)


async def count_keys_with_multiple_current(
    session: AsyncSession, organization_id: uuid.UUID
) -> int:
    """Post-condition check: logical documents that still have >1 current row."""
    result = await session.execute(
        text(
            """
            SELECT count(*) FROM (
                SELECT 1 FROM documents
                WHERE organization_id = :org AND source <> 'manual' AND superseded_at IS NULL
                GROUP BY source, external_id HAVING count(*) > 1
            ) t
            """
        ),
        {"org": organization_id},
    )
    return int(result.scalar_one())


# --- orchestration -----------------------------------------------------------


async def run_backfill(
    session_scope: SessionScope,
    *,
    organization_ids: list[uuid.UUID],
    apply: bool,
    phase: str = "all",
    batch_size: int = 500,
    sleep_seconds: float = 0.2,
    include_soft_deleted: bool = False,
    soft_deleted_before: object | None = None,
    log: Callable[[str], None] = print,
) -> BackfillReport:
    """Report (always) and, when `apply=True`, perform the backfill for the
    given organizations, one short transaction per batch.

    `phase`: "mark", "purge" or "all". `session_scope` is a zero-argument
    callable returning an async context manager that yields a session and
    commits on normal exit (e.g. `app.database.session.session_scope`).
    """
    if phase not in {"mark", "purge", "all"}:
        raise ValueError("phase must be 'mark', 'purge' or 'all'")
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    report = BackfillReport(applied=apply)
    for organization_id in organization_ids:
        org = OrgReport(organization_id=organization_id)
        report.organizations.append(org)

        async with session_scope() as session:
            await begin_org_batch(session, organization_id)
            org.versions_to_mark = await count_versions_to_mark(session, organization_id)
            for table in CHUNK_TABLES:
                org.chunks_to_purge[table] = await count_chunks_to_purge(
                    session, organization_id, table, include_soft_deleted=include_soft_deleted,
                    soft_deleted_before=soft_deleted_before,
                )
        log(
            f"[{organization_id}] backlog: {org.versions_to_mark} version(s) to mark, "
            f"chunks to purge (before marking): {org.chunks_to_purge}"
        )
        if not apply:
            continue

        if phase in {"mark", "all"}:
            after: tuple[str, str] | None = None
            while True:
                async with session_scope() as session:
                    await begin_org_batch(session, organization_id)
                    keys = await keys_needing_mark(
                        session, organization_id, after=after, limit=batch_size
                    )
                    for source, external_id in keys:
                        org.versions_marked += await mark_key_superseded(
                            session, organization_id, source, external_id
                        )
                if not keys:
                    break
                after = keys[-1]
                log(f"[{organization_id}] marked {org.versions_marked} so far")
                await asyncio.sleep(sleep_seconds)

        if phase in {"purge", "all"}:
            for table in CHUNK_TABLES:
                org.chunks_purged[table] = 0
                while True:
                    async with session_scope() as session:
                        await begin_org_batch(session, organization_id)
                        deleted = await purge_chunk_batch(
                            session,
                            organization_id,
                            table,
                            batch_size=batch_size,
                            include_soft_deleted=include_soft_deleted,
                            soft_deleted_before=soft_deleted_before,
                        )
                    if deleted == 0:
                        break
                    org.chunks_purged[table] += deleted
                    await asyncio.sleep(sleep_seconds)
                log(f"[{organization_id}] purged {org.chunks_purged[table]} from {table}")

        async with session_scope() as session:
            await begin_org_batch(session, organization_id)
            org.keys_still_with_multiple_current = await count_keys_with_multiple_current(
                session, organization_id
            )
        log(
            f"[{organization_id}] done: marked={org.versions_marked} "
            f"purged={org.chunks_purged} keys_with_multiple_current="
            f"{org.keys_still_with_multiple_current}"
        )
    return report
