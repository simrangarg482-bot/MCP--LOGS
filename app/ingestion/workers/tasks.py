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

from app.database.session import session_scope
from app.ingestion import repository, service
from app.shared.backoff import full_jitter_backoff_seconds
from app.shared.config.logging import get_logger

logger = get_logger(__name__)

# Bounded exponential backoff, capped at 5 minutes -- PROJECT_PLAN.md
# section 4.5: "exponential backoff per job, with a bounded max-attempt
# count." arq's own default retry (bounded by WorkerSettings.max_tries) has
# no backoff built in -- it just re-runs at the next available worker slot
# -- so this task computes and requests its own defer via `Retry`, rather
# than relying on arq's default behavior to already satisfy this literally.
_MAX_BACKOFF_SECONDS = 300


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
        try:
            async with session_scope() as session:
                job = await service.run_ingestion_job(session, uuid.UUID(connector_config_id))
        except Exception as exc:
            _schedule_retry(connector_config_id, attempt, error=str(exc), cause=exc)
            return

        if job.status == "failed":
            _schedule_retry(
                connector_config_id, attempt, error=f"job failed at stage '{job.failed_stage}'"
            )
            return
        logger.info("ingestion_job_task_completed", job_id=str(job.id), status=job.status)
    finally:
        structlog.contextvars.clear_contextvars()


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
        connector_config_ids = await repository.list_active_connector_config_ids(session)

    redis = ctx["redis"]
    for connector_config_id in connector_config_ids:
        await redis.enqueue_job("run_ingestion_job_task", str(connector_config_id))

    logger.info(
        "ingestion_reconciliation_scheduled",
        connector_config_count=len(connector_config_ids),
    )
