"""Storage snapshots (write) and their read models (tenant estimate, platform overview).

The snapshot query set runs once a day. Per-request endpoints only READ the stored snapshot, so monitoring never
adds an expensive query to a request. See the package docstring for what is measured and what is estimated.
"""

from __future__ import annotations

import uuid
from contextlib import AbstractAsyncContextManager
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PermissionDeniedError, ValidationError
from app.core.tenancy import lifecycle
from app.database.session import set_tenant_context
from app.shared.config.logging import get_logger
from app.shared.config.settings import Settings, get_settings

logger = get_logger(__name__)

SessionScope = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_CHUNK_TABLES = ("documentation_chunks", "code_chunks", "conversations_chunks", "incidents_chunks")
_DOCUMENT_TABLES = ("documents", "document_metadata")
TENANT_BASIS = "row_share_of_chunk_and_document_tables"


def _category(table: str) -> str:
    if table in _CHUNK_TABLES:
        return "chunks"
    if table in _DOCUMENT_TABLES:
        return "documents"
    if table == "audit_logs":
        return "audit"
    return "other"


# ---------------------------------------------------------------------------------------------- write


@dataclass
class SnapshotResult:
    snapshot_id: uuid.UUID
    database_bytes: int
    percent_of_limit: float | None
    warning: bool
    tenants: int
    pruned: int = 0


async def _measure(session: AsyncSession) -> tuple[int, list[tuple]]:
    database_bytes = int((await session.execute(text("SELECT pg_database_size(current_database())"))).scalar_one())
    rows = (
        await session.execute(
            text(
                """
                SELECT c.relname, pg_total_relation_size(c.oid), pg_relation_size(c.oid), pg_indexes_size(c.oid),
                       GREATEST(c.reltuples, 0)::bigint
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
                ORDER BY pg_total_relation_size(c.oid) DESC
                """
            )
        )
    ).all()
    return database_bytes, [tuple(r) for r in rows]


async def take_snapshot(
    session_scope: SessionScope,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> SnapshotResult:
    """Record one snapshot, then per-organization estimates (failures isolated), then prune old snapshots."""
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    limit = settings.database_storage_limit_bytes

    async with session_scope() as session:
        database_bytes, tables = await _measure(session)
        percent = round(database_bytes * 100.0 / limit, 2) if limit else None
        warning = bool(limit) and percent is not None and percent >= settings.storage_warning_percent
        snapshot_id = (
            await session.execute(
                text(
                    "INSERT INTO storage_snapshots (taken_at, database_bytes, storage_limit_bytes, percent_of_limit, "
                    "warning, warning_percent) VALUES (:t, :b, :l, :p, :w, :wp) RETURNING id"
                ),
                {"t": now, "b": database_bytes, "l": limit, "p": percent, "w": warning,
                 "wp": settings.storage_warning_percent if limit else None},
            )
        ).scalar_one()
        for name, total, table_bytes, index_bytes, reltuples in tables:
            await session.execute(
                text(
                    "INSERT INTO storage_snapshot_tables (snapshot_id, table_name, category, total_bytes, table_bytes, "
                    "index_bytes, row_estimate) VALUES (:s, :n, :c, :t, :tb, :ib, :r)"
                ),
                {"s": snapshot_id, "n": name, "c": _category(name), "t": int(total), "tb": int(table_bytes),
                 "ib": int(index_bytes), "r": int(reltuples)},
            )

    if warning:
        logger.warning(
            "database_storage_threshold_exceeded",
            database_bytes=database_bytes,
            limit_bytes=limit,
            percent=percent,
            warning_percent=settings.storage_warning_percent,
        )
    else:
        logger.info("database_storage_snapshot", database_bytes=database_bytes, percent=percent, limit_configured=bool(limit))

    tenants = 0
    if settings.storage_tenant_estimates_enabled:
        tenants = await _snapshot_tenants(session_scope, snapshot_id, tables)

    pruned = 0
    if not settings.retention_dry_run:
        async with session_scope() as session:
            cutoff = now - timedelta(days=settings.storage_snapshot_retention_days)
            result = await session.execute(text("DELETE FROM storage_snapshots WHERE taken_at < :c"), {"c": cutoff})
            pruned = int(result.rowcount or 0)
    return SnapshotResult(snapshot_id, database_bytes, percent, warning, tenants, pruned)


async def _snapshot_tenants(session_scope: SessionScope, snapshot_id: uuid.UUID, tables: list[tuple]) -> int:
    """Count each organization's rows under its own tenant context and store a proportional byte ESTIMATE."""
    sizes = {name: int(total) for name, total, *_ in tables}
    async with session_scope() as session:
        organization_ids = list((await session.execute(text("SELECT id FROM organizations ORDER BY id"))).scalars().all())

    counts: dict[uuid.UUID, dict[str, int]] = {}
    for organization_id in organization_ids:
        try:
            async with session_scope() as session:
                await set_tenant_context(session, organization_id)
                per: dict[str, int] = {}
                for table in (*_CHUNK_TABLES, "documents"):
                    per[table] = int(
                        (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608 - fixed names
                    )
                counts[organization_id] = per
        except Exception:  # noqa: BLE001 - isolation: an organization we could not count simply has no estimate
            logger.exception("storage_tenant_count_failed", organization_id=str(organization_id))

    totals = {t: sum(c[t] for c in counts.values()) for t in (*_CHUNK_TABLES, "documents")}
    stored = 0
    for organization_id, per in counts.items():
        estimate = 0.0
        for table in _CHUNK_TABLES:
            if totals[table]:
                estimate += sizes.get(table, 0) * per[table] / totals[table]
        if totals["documents"]:
            document_bytes = sizes.get("documents", 0) + sizes.get("document_metadata", 0)
            estimate += document_bytes * per["documents"] / totals["documents"]
        try:
            async with session_scope() as session:
                await set_tenant_context(session, organization_id)
                await session.execute(
                    text(
                        "INSERT INTO storage_snapshot_tenants (snapshot_id, organization_id, chunk_rows, document_rows, "
                        "estimated_bytes, basis) VALUES (:s, :o, :cr, :dr, :e, :b)"
                    ),
                    {"s": snapshot_id, "o": organization_id, "cr": sum(per[t] for t in _CHUNK_TABLES),
                     "dr": per["documents"], "e": int(estimate), "b": TENANT_BASIS},
                )
            stored += 1
        except Exception:  # noqa: BLE001
            logger.exception("storage_tenant_estimate_failed", organization_id=str(organization_id))
    return stored


# ---------------------------------------------------------------------------------------------- read models


class TenantStoragePoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    taken_at: datetime
    estimated_bytes: int | None


class TenantStorage(BaseModel):
    """One organization's storage usage. `is_estimate` is always true for bytes: PostgreSQL does not attribute
    table size to tenants (see `basis`). Row counts are exact as of `taken_at`."""

    model_config = ConfigDict(frozen=True)

    available: bool
    taken_at: datetime | None = None
    chunk_rows: int = 0
    document_rows: int = 0
    estimated_bytes: int | None = None
    is_estimate: bool = True
    basis: str = TENANT_BASIS
    history: list[TenantStoragePoint] = []


class TableUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    table: str
    category: str
    total_bytes: int  # measured
    table_bytes: int  # measured
    index_bytes: int  # measured
    row_estimate: int  # planner estimate, not a count


class Growth(BaseModel):
    model_config = ConfigDict(frozen=True)

    days: int
    bytes_added: int  # measured difference between two snapshots
    bytes_per_day: float
    since: datetime


class TenantUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    organization_id: uuid.UUID
    name: str
    slug: str
    estimated_bytes: int | None
    chunk_rows: int
    document_rows: int
    basis: str
    is_estimate: bool = True


class SeriesPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    taken_at: datetime
    database_bytes: int


class PlatformStorage(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool
    taken_at: datetime | None = None
    database_bytes: int = 0  # measured
    storage_limit_bytes: int | None = None  # configured, never guessed
    percent_of_limit: float | None = None
    warning: bool = False
    warning_percent: int | None = None
    categories: dict[str, int] = {}  # measured: summed table totals by category
    tables: list[TableUsage] = []
    growth: list[Growth] = []
    series: list[SeriesPoint] = []
    tenants: list[TenantUsage] = []  # ESTIMATES


async def get_tenant_storage(session: AsyncSession, organization_id: uuid.UUID) -> TenantStorage:
    """The caller's own latest estimate and history (RLS confines the query to the organization)."""
    await set_tenant_context(session, organization_id)
    rows = (
        await session.execute(
            text(
                "SELECT s.taken_at, t.chunk_rows, t.document_rows, t.estimated_bytes "
                "FROM storage_snapshot_tenants t JOIN storage_snapshots s ON s.id = t.snapshot_id "
                "WHERE t.organization_id = :o ORDER BY s.taken_at DESC LIMIT 60"
            ),
            {"o": organization_id},
        )
    ).all()
    if not rows:
        return TenantStorage(available=False)
    latest = rows[0]
    return TenantStorage(
        available=True,
        taken_at=latest[0],
        chunk_rows=int(latest[1]),
        document_rows=int(latest[2]),
        estimated_bytes=int(latest[3]) if latest[3] is not None else None,
        history=[TenantStoragePoint(taken_at=r[0], estimated_bytes=int(r[3]) if r[3] is not None else None) for r in reversed(rows)],
    )


async def get_platform_storage(
    session: AsyncSession, operator_user_id: uuid.UUID, *, now: datetime | None = None, tenant_limit: int = 20
) -> PlatformStorage:
    now = now or datetime.now(timezone.utc)
    latest = (
        await session.execute(
            text(
                "SELECT id, taken_at, database_bytes, storage_limit_bytes, percent_of_limit, warning, warning_percent "
                "FROM storage_snapshots ORDER BY taken_at DESC LIMIT 1"
            )
        )
    ).first()
    if latest is None:
        return PlatformStorage(available=False)
    snapshot_id, taken_at, database_bytes = latest[0], latest[1], int(latest[2])
    tables = (
        await session.execute(
            text(
                "SELECT table_name, category, total_bytes, table_bytes, index_bytes, row_estimate "
                "FROM storage_snapshot_tables WHERE snapshot_id = :s ORDER BY total_bytes DESC"
            ),
            {"s": snapshot_id},
        )
    ).all()
    categories: dict[str, int] = {}
    for r in tables:
        categories[r[1]] = categories.get(r[1], 0) + int(r[2])

    growth: list[Growth] = []
    for days in (7, 30):
        earlier = (
            await session.execute(
                text(
                    "SELECT taken_at, database_bytes FROM storage_snapshots WHERE taken_at <= :c "
                    "ORDER BY taken_at DESC LIMIT 1"
                ),
                {"c": taken_at - timedelta(days=days)},
            )
        ).first()
        if earlier is not None:
            elapsed_days = max((taken_at - earlier[0]).total_seconds() / 86400.0, 1e-9)
            added = database_bytes - int(earlier[1])
            growth.append(Growth(days=days, bytes_added=added, bytes_per_day=round(added / elapsed_days, 1), since=earlier[0]))

    series = (
        await session.execute(
            text("SELECT taken_at, database_bytes FROM storage_snapshots ORDER BY taken_at DESC LIMIT 120")
        )
    ).all()
    try:
        tenants = (
            await session.execute(
                text("SELECT * FROM public.platform_storage_tenants(:op, :n)"), {"op": operator_user_id, "n": tenant_limit}
            )
        ).all()
    except DBAPIError as exc:
        code = lifecycle._sqlstate(exc)  # noqa: SLF001 - the shared SQLSTATE extraction of the platform functions
        if code == "EK003":
            raise PermissionDeniedError("Platform operator access is required.", error_code="platform.operator_required") from exc
        if code == "EK005":
            raise ValidationError("Invalid request.", error_code="platform.invalid_request") from exc
        raise
    return PlatformStorage(
        available=True,
        taken_at=taken_at,
        database_bytes=database_bytes,
        storage_limit_bytes=int(latest[3]) if latest[3] is not None else None,
        percent_of_limit=float(latest[4]) if latest[4] is not None else None,
        warning=bool(latest[5]),
        warning_percent=latest[6],
        categories=categories,
        tables=[TableUsage(table=r[0], category=r[1], total_bytes=int(r[2]), table_bytes=int(r[3]),
                           index_bytes=int(r[4]), row_estimate=int(r[5])) for r in tables],
        growth=growth,
        series=[SeriesPoint(taken_at=r[0], database_bytes=int(r[1])) for r in reversed(series)],
        tenants=[TenantUsage(organization_id=r[0], name=r[1], slug=r[2], estimated_bytes=int(r[3]) if r[3] is not None else None,
                             chunk_rows=int(r[4]), document_rows=int(r[5]), basis=r[6]) for r in tenants],
    )
