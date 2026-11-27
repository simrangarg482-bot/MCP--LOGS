"""Batch-based, tenant-aware, idempotent, failure-isolated retention.

DESIGN
    * Every deletion is `DELETE ... WHERE ctid IN (SELECT ctid ... WHERE <predicate> LIMIT n)`: at most
      `retention_batch_size` rows per transaction, so no long-running transaction and no long lock. Each batch
      is its own short transaction (`SET LOCAL lock_timeout`), followed by a small pause.
    * Tenant-owned tables (`refresh_tokens`, `ingestion_jobs`, `scim_*`) are processed ONE ORGANIZATION AT A
      TIME with that organization's tenant context set, so row-level security confines every statement to the
      organization even if a predicate were wrong. `organizations` has no RLS, so the list of organizations is
      readable without a context. Global tables (`email_verifications`, `password_resets`) have no tenant.
      `mcp_requests` includes rows with no organization (calls that failed before an identity existed); those
      are reachable only through the `purge_mcp_requests_batch` SECURITY DEFINER function.
    * Idempotent: the predicate IS the work queue. A re-run after a crash finds less to do; running twice
      deletes nothing extra.
    * Failure isolation: each (task, organization) runs in its own try/except. One failure is recorded and
      logged; every other task and organization still runs.
    * Dry run (`retention_dry_run`): the same predicates are COUNTED, never deleted.
    * Suspended organizations are NOT skipped: this is housekeeping of expired records, not tenant processing
      (the same category as the operator maintenance scripts in `docs/operations/organization-lifecycle.md`).

WHAT IS NEVER SELECTED (each has a test): a refresh token that is neither expired-past-grace nor
revoked-past-retention; a verification / reset link that has not both expired and aged past its period or been
consumed and aged; a queued or running ingestion job; the newest job of a connector; an unexpired, unrevoked
SCIM token; a still-pending SCIM held action.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import set_tenant_context
from app.shared.config.logging import get_logger
from app.shared.config.settings import Settings, get_settings

logger = get_logger(__name__)

SessionScope = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# Safety valve: a single task never loops more than this many batches in one run (the next run continues).
MAX_BATCHES_PER_TASK = 5000


@dataclass(frozen=True)
class _Spec:
    """One retention rule. `table` and `where` are fixed strings in this module (never user input)."""

    name: str
    table: str
    where: str  # may use :cutoff, :cutoff2 and (tenant-scoped rules) :org
    tenant_scoped: bool


def _specs() -> list[_Spec]:
    return [
        _Spec(
            "refresh_tokens",
            "refresh_tokens",
            # :cutoff = now - expired grace; :cutoff2 = now - revoked retention.
            "organization_id = :org AND (expires_at < :cutoff OR revoked_at < :cutoff2)",
            True,
        ),
        _Spec(
            "ingestion_jobs",
            "ingestion_jobs",
            # Finished jobs only, never a connector's newest job (it drives "last sync" and replay).
            "organization_id = :org AND status IN ('succeeded', 'failed') "
            "AND COALESCE(completed_at, created_at) < :cutoff "
            "AND EXISTS (SELECT 1 FROM ingestion_jobs n WHERE n.connector_config_id = ingestion_jobs.connector_config_id "
            "AND (n.created_at, n.id) > (ingestion_jobs.created_at, ingestion_jobs.id))",
            True,
        ),
        _Spec(
            "scim_tokens",
            "scim_tokens",
            "organization_id = :org AND (revoked_at < :cutoff OR expires_at < :cutoff)",
            True,
        ),
        _Spec(
            "scim_pending_actions",
            "scim_pending_actions",
            "organization_id = :org AND status IN ('approved', 'rejected') AND decided_at < :cutoff",
            True,
        ),
        _Spec(
            "email_verifications",
            "email_verifications",
            "(expires_at < :cutoff) OR (consumed_at < :cutoff)",
            False,
        ),
        _Spec(
            "password_resets",
            "password_resets",
            "(expires_at < :cutoff) OR (consumed_at < :cutoff)",
            False,
        ),
    ]


@dataclass
class TaskResult:
    task: str
    deleted: int = 0
    would_delete: int = 0
    organizations: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class RetentionReport:
    dry_run: bool
    results: list[TaskResult] = field(default_factory=list)

    @property
    def total_deleted(self) -> int:
        return sum(r.deleted for r in self.results)

    @property
    def errors(self) -> list[str]:
        return [e for r in self.results for e in r.errors]


def _cutoffs(spec: _Spec, settings: Settings, now: datetime) -> dict[str, datetime]:
    days = timedelta
    if spec.name == "refresh_tokens":
        return {
            "cutoff": now - days(days=settings.refresh_token_expired_grace_days),
            "cutoff2": now - days(days=settings.refresh_token_revoked_retention_days),
        }
    if spec.name == "ingestion_jobs":
        return {"cutoff": now - days(days=settings.ingestion_job_retention_days)}
    if spec.name == "scim_tokens":
        return {"cutoff": now - days(days=settings.scim_token_retention_days)}
    if spec.name == "scim_pending_actions":
        return {"cutoff": now - days(days=settings.scim_pending_action_retention_days)}
    if spec.name == "email_verifications":
        return {"cutoff": now - days(days=settings.email_verification_retention_days)}
    if spec.name == "password_resets":
        return {"cutoff": now - days(days=settings.password_reset_retention_days)}
    raise ValueError(spec.name)  # pragma: no cover - the specs above are exhaustive


async def _begin(session: AsyncSession, spec_org: uuid.UUID | None) -> None:
    if spec_org is not None:
        await set_tenant_context(session, spec_org)
    await session.execute(text("SET LOCAL lock_timeout = '5s'"))


async def _delete_batch(
    session: AsyncSession, spec: _Spec, params: dict, batch_size: int
) -> int:
    result = await session.execute(
        text(
            f"DELETE FROM {spec.table} WHERE ctid IN "  # noqa: S608 - fixed table/predicate strings
            f"(SELECT ctid FROM {spec.table} WHERE {spec.where} LIMIT :batch)"
        ),
        {**params, "batch": batch_size},
    )
    return int(result.rowcount or 0)


async def _count(session: AsyncSession, spec: _Spec, params: dict) -> int:
    result = await session.execute(
        text(f"SELECT count(*) FROM {spec.table} WHERE {spec.where}"), params  # noqa: S608
    )
    return int(result.scalar_one())


async def _run_one(
    session_scope: SessionScope,
    spec: _Spec,
    organization_id: uuid.UUID | None,
    settings: Settings,
    now: datetime,
    sleep: Callable[[float], Awaitable[None]],
) -> tuple[int, int]:
    """Returns (deleted, would_delete) for one rule and one organization (or the global table)."""
    params: dict = _cutoffs(spec, settings, now)
    if organization_id is not None:
        params["org"] = organization_id
    if settings.retention_dry_run:
        async with session_scope() as session:
            await _begin(session, organization_id)
            return 0, await _count(session, spec, params)
    deleted = 0
    for _ in range(MAX_BATCHES_PER_TASK):
        async with session_scope() as session:
            await _begin(session, organization_id)
            n = await _delete_batch(session, spec, params, settings.retention_batch_size)
        deleted += n
        if n < settings.retention_batch_size:
            break
        await sleep(settings.retention_batch_pause_seconds)
    return deleted, 0


async def _purge_mcp_requests(
    session_scope: SessionScope, settings: Settings, now: datetime, sleep: Callable[[float], Awaitable[None]]
) -> tuple[int, int]:
    cutoff = now - timedelta(days=settings.mcp_request_retention_days)
    if settings.retention_dry_run:
        async with session_scope() as session:
            # The application role cannot count other tenants' rows (RLS); the definer function is the only
            # cross-tenant path, so a dry run reports nothing countable rather than guessing.
            return 0, 0
    deleted = 0
    for _ in range(MAX_BATCHES_PER_TASK):
        async with session_scope() as session:
            await session.execute(text("SET LOCAL lock_timeout = '5s'"))
            result = await session.execute(
                text("SELECT public.purge_mcp_requests_batch(:cutoff, :batch)"),
                {"cutoff": cutoff, "batch": settings.retention_batch_size},
            )
            n = int(result.scalar_one() or 0)
        deleted += n
        if n < settings.retention_batch_size:
            break
        await sleep(settings.retention_batch_pause_seconds)
    return deleted, 0


async def list_organization_ids(session_scope: SessionScope) -> list[uuid.UUID]:
    async with session_scope() as session:
        result = await session.execute(text("SELECT id FROM organizations ORDER BY id"))
        return list(result.scalars().all())


async def run_retention(
    session_scope: SessionScope,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    only: set[str] | None = None,
) -> RetentionReport:
    """Run every retention rule once. Never raises for a single rule's or organization's failure: those are
    collected in the returned report (and logged), and everything else still runs.
    `only` limits the run to the named rules (used by tests and by `scripts/run_retention.py --only`)."""
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    report = RetentionReport(dry_run=settings.retention_dry_run)

    def wanted(name: str) -> bool:
        return only is None or name in only

    if wanted("mcp_requests"):
        result = TaskResult("mcp_requests")
        try:
            result.deleted, result.would_delete = await _purge_mcp_requests(session_scope, settings, now, sleep)
        except Exception as exc:  # noqa: BLE001 - isolation: record, log, carry on
            result.errors.append(f"mcp_requests: {type(exc).__name__}")
            logger.exception("retention_task_failed", task="mcp_requests")
        report.results.append(result)

    try:
        organization_ids = await list_organization_ids(session_scope)
    except Exception as exc:  # noqa: BLE001
        organization_ids = []
        report.results.append(TaskResult("organizations", errors=[f"list_organizations: {type(exc).__name__}"]))
        logger.exception("retention_task_failed", task="list_organizations")

    for spec in _specs():
        if not wanted(spec.name):
            continue
        result = TaskResult(spec.name)
        if spec.tenant_scoped:
            for organization_id in organization_ids:
                try:
                    deleted, would = await _run_one(session_scope, spec, organization_id, settings, now, sleep)
                    result.deleted += deleted
                    result.would_delete += would
                    result.organizations += 1
                except Exception as exc:  # noqa: BLE001 - one organization's failure never stops the rest
                    result.errors.append(f"{spec.name}[{organization_id}]: {type(exc).__name__}")
                    logger.exception(
                        "retention_task_failed", task=spec.name, organization_id=str(organization_id)
                    )
        else:
            try:
                result.deleted, result.would_delete = await _run_one(session_scope, spec, None, settings, now, sleep)
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"{spec.name}: {type(exc).__name__}")
                logger.exception("retention_task_failed", task=spec.name)
        report.results.append(result)

    for r in report.results:
        logger.info(
            "retention_task_completed",
            task=r.task,
            deleted=r.deleted,
            would_delete=r.would_delete,
            organizations=r.organizations,
            errors=len(r.errors),
            dry_run=report.dry_run,
        )
    return report
