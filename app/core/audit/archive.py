"""Audit-log archival: a verified, non-destructive export of old audit history.

WHAT THIS DOES -- AND DELIBERATELY DOES NOT DO
    * It EXPORTS one organization's audit rows for one calendar month to a compressed NDJSON file under
      `AUDIT_ARCHIVE_DIRECTORY`, verifies the file by reading it back (row count, unique ids, SHA-256), and
      records that fact in `audit_archive_batches`.
    * **It never deletes, updates or rewrites an `audit_logs` row.** The table, its indexes and every audit API
      are untouched, so audit query behaviour cannot change. Removing exported rows (or detaching a partition)
      is a separate, explicitly approved step that needs durable storage this repository does not have; until
      then the ledger simply proves an off-table copy exists. See docs/operations/data-retention.md.
    * One file per (organization, month): tenants are never mixed in a file, and the export runs under that
      organization's tenant context, so row-level security confines every read.
    * Failure-safe and resumable: the file is written to a temporary name and renamed only after verification;
      a crash leaves at most a `.tmp` file (removed on the next attempt) and never a half-written archive. A
      failed month is recorded as `failed` and retried on the next run; a verified month is skipped.

ROWS WITH NO ORGANIZATION (`audit_logs.organization_id IS NULL`) are invisible to the application role (row-level
security) and are not produced by any code path today; they are out of scope for this tool.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import set_tenant_context
from app.shared.config.logging import get_logger
from app.shared.config.settings import Settings, get_settings

logger = get_logger(__name__)

SessionScope = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass
class ArchiveOutcome:
    organization_id: uuid.UUID
    period_start: date
    status: str  # verified | skipped | empty | planned | failed
    row_count: int = 0
    location: str | None = None
    sha256: str | None = None
    error: str | None = None


@dataclass
class ArchiveReport:
    applied: bool
    outcomes: list[ArchiveOutcome] = field(default_factory=list)

    @property
    def failed(self) -> list[ArchiveOutcome]:
        return [o for o in self.outcomes if o.status == "failed"]


def month_bounds(period_start: date) -> tuple[date, date]:
    start = period_start.replace(day=1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end


def archive_path(directory: Path, organization_id: uuid.UUID, period_start: date) -> Path:
    return directory / str(organization_id) / f"{period_start:%Y-%m}.ndjson.gz"


def _line(row) -> bytes:
    return (
        json.dumps(
            {
                "id": str(row.id),
                "organization_id": str(row.organization_id),
                "actor": row.actor,
                "action": row.action,
                "resource_type": row.resource_type,
                "resource_id": str(row.resource_id) if row.resource_id else None,
                "metadata": row.metadata,
                "occurred_at": row.occurred_at.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )


async def _range_count(session: AsyncSession, organization_id: uuid.UUID, start: date, end: date) -> int:
    result = await session.execute(
        text("SELECT count(*) FROM audit_logs WHERE organization_id = :o AND occurred_at >= :s AND occurred_at < :e"),
        {"o": organization_id, "s": datetime.combine(start, datetime.min.time(), timezone.utc),
         "e": datetime.combine(end, datetime.min.time(), timezone.utc)},
    )
    return int(result.scalar_one())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_file(path: Path, *, expected_rows: int, expected_sha256: str | None = None) -> tuple[bool, str | None]:
    """Read an archive back: it must exist, hash to the recorded SHA-256, hold exactly `expected_rows` lines and
    no duplicated ids. Returns (ok, reason)."""
    if not path.is_file():
        return False, "file missing"
    if expected_sha256 is not None and _sha256(path) != expected_sha256:
        return False, "checksum mismatch"
    ids: set[str] = set()
    count = 0
    try:
        with gzip.open(path, "rb") as handle:
            for raw in handle:
                ids.add(json.loads(raw)["id"])
                count += 1
    except (OSError, ValueError, KeyError) as exc:
        return False, f"unreadable: {type(exc).__name__}"
    if count != expected_rows or len(ids) != count:
        return False, f"row count {count} (unique {len(ids)}) != expected {expected_rows}"
    return True, None


async def _record(
    session_scope: SessionScope, organization_id: uuid.UUID, start: date, end: date, *, status: str,
    row_count: int, location: str | None, sha256: str | None, byte_size: int | None, error: str | None,
) -> None:
    async with session_scope() as session:
        await set_tenant_context(session, organization_id)
        await session.execute(
            text(
                """
                INSERT INTO audit_archive_batches (organization_id, period_start, period_end, row_count, byte_size, sha256,
                                                   location, status, error, exported_at, verified_at)
                VALUES (:o, :s, :e, :n, :b, :h, :l, :st, :err, now(), CASE WHEN :st = 'verified' THEN now() END)
                ON CONFLICT (organization_id, period_start) DO UPDATE SET
                    row_count = EXCLUDED.row_count, byte_size = EXCLUDED.byte_size, sha256 = EXCLUDED.sha256,
                    location = EXCLUDED.location, status = EXCLUDED.status, error = EXCLUDED.error,
                    exported_at = now(), verified_at = EXCLUDED.verified_at
                """
            ),
            {"o": organization_id, "s": start, "e": end, "n": row_count, "b": byte_size, "h": sha256, "l": location,
             "st": status, "err": error},
        )


async def archive_month(
    session_scope: SessionScope,
    organization_id: uuid.UUID,
    period_start: date,
    *,
    directory: Path,
    now: datetime | None = None,
    batch_size: int = 5000,
) -> ArchiveOutcome:
    """Export, verify and record one month. Never deletes an audit row."""
    now = now or datetime.now(timezone.utc)
    start, end = month_bounds(period_start)
    if datetime.combine(end, datetime.min.time(), timezone.utc) > now:
        raise ValueError("only a month that has completely ended can be archived")
    path = archive_path(directory, organization_id, start)
    tmp = path.with_name(path.name + ".tmp")
    s_ts = datetime.combine(start, datetime.min.time(), timezone.utc)
    e_ts = datetime.combine(end, datetime.min.time(), timezone.utc)

    async with session_scope() as session:
        await set_tenant_context(session, organization_id)
        expected = await _range_count(session, organization_id, start, end)
        existing = (
            await session.execute(
                text("SELECT status, row_count, sha256, location FROM audit_archive_batches "
                     "WHERE organization_id = :o AND period_start = :s"),
                {"o": organization_id, "s": start},
            )
        ).first()
    if expected == 0:
        return ArchiveOutcome(organization_id, start, "empty")
    if existing is not None and existing.status == "verified" and int(existing.row_count) == expected:
        ok, _reason = verify_file(path, expected_rows=expected, expected_sha256=existing.sha256)
        if ok:
            return ArchiveOutcome(organization_id, start, "skipped", expected, str(path), existing.sha256)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if tmp.exists():
            tmp.unlink()  # a previous attempt died before the rename
        written = 0
        after: tuple[datetime, uuid.UUID] | None = None
        with open(tmp, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as out:  # mtime=0: reproducible bytes
            while True:
                async with session_scope() as session:
                    await set_tenant_context(session, organization_id)
                    params: dict = {"o": organization_id, "s": s_ts, "e": e_ts, "n": batch_size}
                    cursor = ""
                    if after is not None:
                        cursor = "AND (occurred_at, id) > (:ao, :ai)"
                        params["ao"], params["ai"] = after
                    rows = (
                        await session.execute(
                            text(
                                "SELECT id, organization_id, actor, action, resource_type, resource_id, metadata, occurred_at "
                                "FROM audit_logs WHERE organization_id = :o AND occurred_at >= :s AND occurred_at < :e "
                                f"{cursor} ORDER BY occurred_at, id LIMIT :n"  # noqa: S608 - fixed fragment
                            ),
                            params,
                        )
                    ).all()
                if not rows:
                    break
                for row in rows:
                    out.write(_line(row))
                written += len(rows)
                after = (rows[-1].occurred_at, rows[-1].id)
            raw.flush()
            os.fsync(raw.fileno())
        async with session_scope() as session:
            await set_tenant_context(session, organization_id)
            final_count = await _range_count(session, organization_id, start, end)
        if written != final_count:
            raise RuntimeError(f"row count changed during export ({written} written, {final_count} present)")
        digest = _sha256(tmp)
        ok, reason = verify_file(tmp, expected_rows=written, expected_sha256=digest)
        if not ok:
            raise RuntimeError(f"verification failed: {reason}")
        os.replace(tmp, path)  # atomic: the archive appears only once it is complete and verified
        await _record(session_scope, organization_id, start, end, status="verified", row_count=written,
                      location=str(path), sha256=digest, byte_size=path.stat().st_size, error=None)
        logger.info("audit_archive_verified", organization_id=str(organization_id), period=f"{start:%Y-%m}", rows=written)
        return ArchiveOutcome(organization_id, start, "verified", written, str(path), digest)
    except Exception as exc:  # noqa: BLE001 - recorded, retried on the next run
        if tmp.exists():
            tmp.unlink()
        logger.exception("audit_archive_failed", organization_id=str(organization_id), period=f"{start:%Y-%m}")
        try:
            await _record(session_scope, organization_id, start, end, status="failed", row_count=expected,
                          location=None, sha256=None, byte_size=None, error=type(exc).__name__)
        except Exception:  # noqa: BLE001
            logger.exception("audit_archive_ledger_failed", organization_id=str(organization_id))
        return ArchiveOutcome(organization_id, start, "failed", expected, None, None, type(exc).__name__)


async def months_to_archive(
    session_scope: SessionScope, organization_id: uuid.UUID, *, now: datetime, min_age_days: int
) -> list[date]:
    """Calendar months that ended at least `min_age_days` ago and have audit rows."""
    cutoff = now - timedelta(days=min_age_days)
    async with session_scope() as session:
        await set_tenant_context(session, organization_id)
        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT date_trunc('month', occurred_at AT TIME ZONE 'UTC')::date FROM audit_logs "
                    "WHERE organization_id = :o AND occurred_at < :c ORDER BY 1"
                ),
                {"o": organization_id, "c": cutoff},
            )
        ).scalars().all()
    months = []
    for first in rows:
        _start, end = month_bounds(first)
        if datetime.combine(end, datetime.min.time(), timezone.utc) <= cutoff:
            months.append(first)
    return months


async def run_archive(
    session_scope: SessionScope,
    *,
    organization_ids: list[uuid.UUID] | None = None,
    settings: Settings | None = None,
    apply: bool = False,
    now: datetime | None = None,
) -> ArchiveReport:
    """Archive every eligible month of the given organizations (default: all). Dry run unless `apply`."""
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    if settings.audit_archive_directory is None:
        raise ValueError("AUDIT_ARCHIVE_DIRECTORY is not configured: archiving needs durable storage")
    directory = Path(settings.audit_archive_directory)
    report = ArchiveReport(applied=apply)
    if organization_ids is None:
        async with session_scope() as session:
            organization_ids = list((await session.execute(text("SELECT id FROM organizations ORDER BY id"))).scalars().all())
    for organization_id in organization_ids:
        try:
            months = await months_to_archive(
                session_scope, organization_id, now=now, min_age_days=settings.audit_archive_min_age_days
            )
        except Exception as exc:  # noqa: BLE001 - one organization's failure never stops the rest
            logger.exception("audit_archive_plan_failed", organization_id=str(organization_id))
            report.outcomes.append(ArchiveOutcome(organization_id, now.date(), "failed", error=type(exc).__name__))
            continue
        for month in months:
            if not apply:
                report.outcomes.append(ArchiveOutcome(organization_id, month, "planned"))
                continue
            report.outcomes.append(
                await archive_month(session_scope, organization_id, month, directory=directory, now=now)
            )
    return report


async def verify_archives(
    session_scope: SessionScope, organization_id: uuid.UUID
) -> list[tuple[date, bool, str | None]]:
    """Re-check every recorded archive of an organization against its file."""
    async with session_scope() as session:
        await set_tenant_context(session, organization_id)
        rows = (
            await session.execute(
                text("SELECT period_start, row_count, sha256, location FROM audit_archive_batches "
                     "WHERE organization_id = :o AND status = 'verified' ORDER BY period_start"),
                {"o": organization_id},
            )
        ).all()
    results = []
    for r in rows:
        ok, reason = verify_file(Path(r.location), expected_rows=int(r.row_count), expected_sha256=r.sha256)
        results.append((r.period_start, ok, reason))
    return results
