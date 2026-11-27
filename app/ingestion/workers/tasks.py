"""arq task functions -- the actual units of work the ingestion worker
process runs (PROJECT_PLAN.md section 4.5, ENGINEERING_DECISIONS.md #002).

Owned by: ingestion/workers/. Thin wrappers only: each task opens its own
database session via `session_scope()` (arq jobs run in a separate process
with no FastAPI request to inject one from, per `database.session`'s own
docstring on why `session_scope()` exists) and delegates immediately to
`ingestion.service`. No business logic lives here.
"""

from __future__ import annotations

import uuid

import structlog
from arq.worker import Retry

from app.core.exceptions import EKIPError, OrganizationSuspendedError
from app.database.session import session_scope
from app.core.retention import service as retention_service
from app.core.storage import service as storage_service
from app.ingestion import repository, service
from app.ingestion import retention as document_retention
from app.shared.backoff import full_jitter_backoff_seconds
from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings
from app.shared.distributed_rate_limiter import RedisTokenBucketRateLimiter

logger = get_logger(__name__)

# Bounded exponential backoff, capped at 5 minutes -- PROJECT_PLAN.md
# section 4.5: "exponential backoff per job, with a bounded max-attempt
# count." arq's own default retry (bounded by WorkerSettings.max_tries) has
# no backoff built in -- it just re-runs at the next available worker slot
# -- so this task computes and requests its own defer via `Retry`, rather
# than relying on arq's default behavior to already satisfy this literally.
_MAX_BACKOFF_SECONDS = 300
MAX_JOB_TRIES = 3
_LOCK_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


async def _acquire_connector_lock(
    ctx: dict, connector_config_id: str
) -> tuple[object, str, str] | None:
    """Acquire a crash-safe, cross-process lock for one connector sync.

    API clicks and hourly reconciliation can enqueue the same connector at
    once, and production commonly runs more than one worker replica. The
    token prevents one worker from releasing another's lock; the TTL makes a
    hard process death self-healing. Direct unit invocations without ARQ's
    Redis context retain the pre-lock behavior.
    """
    redis = ctx.get("redis")
    if redis is None:
        logger.warning("ingestion_connector_lock_unavailable", connector_config_id=connector_config_id)
        return (None, "", "")
    key = f"ekip:ingestion:lock:{connector_config_id}"
    token = f"{ctx.get('job_id', '')}:{uuid.uuid4()}"
    ttl = get_settings().ingestion_job_timeout_seconds + _MAX_BACKOFF_SECONDS
    acquired = await redis.set(key, token, ex=ttl, nx=True)
    if not acquired:
        return None
    return redis, key, token


async def _release_connector_lock(lock: tuple[object, str, str]) -> None:
    redis, key, token = lock
    if redis is None:
        return
    try:
        await redis.eval(_LOCK_RELEASE_SCRIPT, 1, key, token)
    except Exception:
        # The TTL is the recovery mechanism if Redis disappears during
        # cleanup. Never replace the job's real outcome with a lock cleanup
        # exception.
        logger.warning("ingestion_connector_lock_release_failed", lock_key=key, exc_info=True)


async def run_ingestion_job_task(ctx: dict, connector_config_id: str) -> None:
    """Run one ingestion job for `connector_config_id` (passed as `str`,
    since arq job arguments are serialized -- converted back to a UUID
    here, at the boundary, not inside `ingestion.service`).

    Two distinct failure shapes, handled two different ways:

    1. `service.run_ingestion_job` raises -- only possible today for a
       setup-phase failure before any `ingestion_jobs` row exists (e.g. the
       `connector_config_id` was deleted between enqueue and run), so
       there's nothing in the database to have recorded the failure. Caught
       below and turned into a `Retry`.
    2. `service.run_ingestion_job` returns normally with `job.status ==
       "failed"` -- a failure *inside* the fetch/process loop, which
       `ingestion.service._execute_ingestion_job` deliberately does not
       re-raise (see that function's own comment): re-raising would let
       the exception reach `session_scope()`'s rollback and erase the very
       `status="failed"`/`failed_stage` write meant to make the failure
       durable. This branch is what turns that recorded-but-not-exceptional
       outcome into the same `Retry` behavior.
    """
    attempt = ctx["job_try"]
    # Phase 5.2: same request-correlation shape as the REST/MCP entry
    # points -- arq's own `job_id` is the natural request_id here (already
    # unique per enqueued job, no need to mint a second one), bound before
    # `service.run_ingestion_job` runs so every log line inside it (and any
    # retrieval/embedding call it makes) carries it automatically.
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=str(ctx.get("job_id", "")) or None,
        connector_config_id=connector_config_id,
    )
    try:
        lock = await _acquire_connector_lock(ctx, connector_config_id)
    except Exception as exc:
        if attempt >= MAX_JOB_TRIES:
            _log_exhausted(connector_config_id, attempt, error=f"lock acquisition failed: {exc}")
            structlog.contextvars.clear_contextvars()
            return
        _schedule_retry(connector_config_id, attempt, error=f"lock acquisition failed: {exc}", cause=exc)
        return
    if lock is None:
        logger.info(
            "ingestion_job_task_skipped_duplicate",
            connector_config_id=connector_config_id,
            attempt=attempt,
        )
        structlog.contextvars.clear_contextvars()
        return
    try:
        try:
            async with session_scope() as session:
                rate_limit_kwargs = (
                    {
                        "rate_limiter": RedisTokenBucketRateLimiter(ctx["redis"]),
                        "attempt_number": attempt,
                    }
                    if ctx.get("redis") is not None
                    else {}
                )
                job = await service.run_ingestion_job(
                    session,
                    uuid.UUID(connector_config_id),
                    **rate_limit_kwargs,
                )
                if (
                    job.status == "failed"
                    and job.failed_stage != service.ORGANIZATION_SUSPENDED_STAGE
                    and attempt >= MAX_JOB_TRIES
                ):
                    job = await service.dead_letter_ingestion_job(
                        session, job.id, job.organization_id
                    )
        except EKIPError as exc:
            if isinstance(exc, OrganizationSuspendedError):
                # Not a failure and not worth a retry: the organization was suspended after this
                # job was queued. Nothing was read, decrypted or fetched for it.
                logger.info(
                    "ingestion_job_task_skipped_organization_suspended",
                    connector_config_id=connector_config_id,
                    attempt=attempt,
                )
                return
            if exc.error_code == "connector_config.disconnected":
                # Deliberately NOT a `_schedule_retry` case: this is not a
                # transient failure to back off from, it's the user having
                # deleted the connector (`core.tenancy.service.
                # disconnect_connector`) since this job was originally
                # enqueued or since its last attempt timed out. Retrying
                # would just re-hit the exact same guard `attempt` more
                # times, burning `_MAX_BACKOFF_SECONDS`-scale delays for
                # nothing -- logging and returning cleanly stops it here,
                # on the very next attempt, instead of at `max_tries`.
                logger.info(
                    "ingestion_job_task_skipped_disconnected",
                    connector_config_id=connector_config_id,
                    attempt=attempt,
                )
                return
            if attempt >= MAX_JOB_TRIES:
                _log_exhausted(connector_config_id, attempt, error=str(exc))
                return
            _schedule_retry(connector_config_id, attempt, error=str(exc), cause=exc)
            return
        except Exception as exc:
            if attempt >= MAX_JOB_TRIES:
                _log_exhausted(connector_config_id, attempt, error=str(exc))
                return
            _schedule_retry(connector_config_id, attempt, error=str(exc), cause=exc)
            return

        if job.status == "failed" and job.failed_stage == service.ORGANIZATION_SUSPENDED_STAGE:
            # Stopped at a page boundary because the organization was suspended mid-run: a clear
            # terminal outcome. Never retried and never dead-lettered; the cursor is preserved.
            logger.info(
                "ingestion_job_task_stopped_organization_suspended",
                job_id=str(job.id),
                connector_config_id=connector_config_id,
                attempt=attempt,
            )
            return
        if job.status == "dead_lettered":
            logger.error(
                "ingestion_job_dead_lettered",
                job_id=str(job.id),
                connector_config_id=connector_config_id,
                attempt=attempt,
                failed_stage=job.failed_stage,
            )
            return
        if job.status == "failed":
            _schedule_retry(
                connector_config_id, attempt, error=f"job failed at stage '{job.failed_stage}'"
            )
            return
        logger.info("ingestion_job_task_completed", job_id=str(job.id), status=job.status)
    finally:
        await _release_connector_lock(lock)
        structlog.contextvars.clear_contextvars()


def _log_exhausted(connector_config_id: str, attempt: int, *, error: str) -> None:
    logger.error(
        "ingestion_job_exhausted_before_start",
        connector_config_id=connector_config_id,
        attempt=attempt,
        error=error,
    )


def _schedule_retry(
    connector_config_id: str, attempt: int, *, error: str, cause: BaseException | None = None
) -> None:
    # Phase 6.2: full jitter, not a bare `min(2**attempt, cap)` -- a
    # correlated failure (a connector-wide outage affecting every
    # organization syncing it at once) previously meant every affected job
    # retried at the exact same intervals, arriving back in synchronized
    # waves rather than spread out. See `app.shared.backoff`'s own
    # docstring for why "full jitter" specifically.
    defer_seconds = full_jitter_backoff_seconds(attempt, cap=_MAX_BACKOFF_SECONDS)
    logger.warning(
        "ingestion_job_task_retry_scheduled",
        connector_config_id=connector_config_id,
        attempt=attempt,
        defer_seconds=defer_seconds,
        error=error,
    )
    raise Retry(defer=defer_seconds) from cause


async def scheduled_reconciliation(ctx: dict) -> None:
    """Periodic reconciliation pass (PROJECT_PLAN.md section 4.4): enqueues
    a sync job for every active (or previously-errored) connector_config,
    catching anything a missed/failed webhook delivery would otherwise
    silently drop -- even for webhook-supported sources.

    Enqueues rather than running jobs inline, so one organization's large
    sync can't block this scan from reaching every other organization's
    connectors. Each enqueued job is rate-limited per `connector_config` and
    per organization (PROJECT_PLAN.md section 4.5/Milestone 10) inside
    `ingestion.service._execute_ingestion_job` itself, via
    `app.shared.rate_limiter` -- not attempted here, since this function
    only enqueues jobs, it never runs one.

    Milestone 10 RLS note: `repository.list_active_connector_config_ids`
    goes through a narrow RLS-bypassing SQL function rather than a normal
    scoped query -- this scan is deliberately cross-tenant (it must reach
    every organization's connectors, not just one), the same exception
    `core.tenancy.repository.list_organizations` already is for the
    Knowledge Gap Agent's scan. Only ids come back, never full rows; each
    resulting job resolves and scopes to its own connector_config's
    organization independently, inside `_execute_ingestion_job`.
    """
    async with session_scope() as session:
        # Suspended organizations' connectors are dropped HERE, before anything is enqueued (the
        # worker re-checks at start too). One suspended organization changes nothing for the rest.
        connector_config_ids, skipped_suspended = await service.list_reconciliation_connector_config_ids(
            session
        )

    redis = ctx["redis"]
    for connector_config_id in connector_config_ids:
        await redis.enqueue_job("run_ingestion_job_task", str(connector_config_id))

    logger.info(
        "ingestion_reconciliation_scheduled",
        connector_config_count=len(connector_config_ids),
        skipped_suspended_organizations=skipped_suspended,
    )


async def cleanup_connector_documents_task(ctx: dict, organization_id: str, connector_config_id: str) -> dict:
    """Remove a DISCONNECTED connector's documents and chunks, in batches, off the request path.

    Enqueued by `DELETE /tenancy/connectors/{id}` after the disconnect is committed. Idempotent and retryable
    (a transient failure is retried with backoff; if every attempt fails the daily maintenance job's connector
    sweep finishes it). A connector that is not disconnected, or not this organization's, is a no-op.
    """
    settings = get_settings()
    attempt = int(ctx.get("job_try", 1))
    try:
        outcome = await document_retention.cleanup_connector_documents(
            session_scope,
            uuid.UUID(organization_id),
            uuid.UUID(connector_config_id),
            batch_size=settings.retention_batch_size,
            pause_seconds=settings.retention_batch_pause_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "connector_cleanup_task_failed", connector_config_id=connector_config_id, attempt=attempt
        )
        if attempt < MAX_JOB_TRIES:
            raise Retry(defer=min(_MAX_BACKOFF_SECONDS, 30 * 2**attempt)) from exc
        return {"status": "failed", "retry": "daily_sweep"}
    if outcome is None:
        return {"status": "skipped"}
    return {"status": "cleaned", "documents_deleted": outcome[0], "chunks_deleted": outcome[1]}


async def scheduled_maintenance(ctx: dict) -> dict:
    """Daily maintenance: retention of temporary records, then document/chunk cleanup. Each step is isolated:
    a failure in one is logged and never prevents the next. `RETENTION_ENABLED=false` switches it all off and
    `RETENTION_DRY_RUN=true` only counts. The last step records a storage snapshot (`core.storage`)."""
    settings = get_settings()
    if not settings.retention_enabled:
        logger.info("maintenance_skipped", reason="retention_disabled")
        return {"status": "disabled"}
    summary: dict[str, object] = {"dry_run": settings.retention_dry_run}
    try:
        report = await retention_service.run_retention(session_scope, settings=settings)
        summary["retention_deleted"] = report.total_deleted
        summary["retention_errors"] = len(report.errors)
    except Exception:  # noqa: BLE001
        logger.exception("maintenance_step_failed", step="retention")
        summary["retention_error"] = True
    try:
        documents = await document_retention.run_document_cleanup(session_scope, settings=settings)
        summary["document_chunks_purged"] = sum(documents.chunks_purged.values())
        summary["versions_tombstoned"] = documents.versions_tombstoned
        summary["document_errors"] = len(documents.errors)
    except Exception:  # noqa: BLE001
        logger.exception("maintenance_step_failed", step="documents")
        summary["documents_error"] = True
    try:
        snapshot = await storage_service.take_snapshot(session_scope, settings=settings)
        summary["database_bytes"] = snapshot.database_bytes
        summary["storage_warning"] = snapshot.warning
    except Exception:  # noqa: BLE001
        logger.exception("maintenance_step_failed", step="storage_snapshot")
        summary["storage_error"] = True
    logger.info("maintenance_completed", **summary)
    return summary
