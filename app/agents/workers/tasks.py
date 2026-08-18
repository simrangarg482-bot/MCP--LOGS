"""arq task functions for agents/workers -- the Knowledge Gap Agent's
scheduled entry points.

Owned by: agents/workers. Same "thin wrapper, open own session, delegate
immediately" discipline as `app.ingestion.workers.tasks`'s module docstring
describes for its own tasks -- no business logic lives here, only session
lifecycle and arq retry/backoff bookkeeping.
"""

from __future__ import annotations

import uuid

import structlog
from arq.worker import Retry

from app.agents import service as agents_service
from app.core.tenancy import service as tenancy_service
from app.database.session import session_scope, set_tenant_context
from app.shared.backoff import full_jitter_backoff_seconds
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity

logger = get_logger(__name__)

# Same bounded exponential backoff shape as
# `app.ingestion.workers.tasks.run_ingestion_job_task` -- see that
# function's docstring for why this task computes and requests its own
# defer via `Retry` rather than relying on arq's default (backoff-free)
# retry behavior.
_MAX_BACKOFF_SECONDS = 300


async def run_knowledge_gap_detection_task(ctx: dict, organization_id: str) -> None:
    """Run the Knowledge Gap Agent for one organization (passed as `str`,
    since arq job arguments are serialized -- converted back to a UUID here,
    at the boundary).

    Constructs `Identity.for_agent("knowledge_gap_agent", organization_id)`
    -- see `agents.service.detect_knowledge_gaps`'s docstring for why this
    mirrors `core.tenancy.service.update_connector_sync_status`'s identical
    precedent for a scheduled worker's system-triggered identity.

    Milestone 10 RLS note: unlike `app.ingestion.service._execute_ingestion_job`,
    this task already knows its `organization_id` from its own arq job
    argument -- no chicken-and-egg lookup needed -- so `set_tenant_context`
    is called immediately after opening the session, before the single
    `detect_knowledge_gaps` call that queries every RLS-protected table this
    pipeline touches.
    """
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=str(ctx.get("job_id", "")) or None,
        organization_id=organization_id,
    )
    try:
        try:
            async with session_scope() as session:
                org_uuid = uuid.UUID(organization_id)
                await set_tenant_context(session, org_uuid)
                actor = Identity.for_agent("knowledge_gap_agent", org_uuid)
                reports = await agents_service.detect_knowledge_gaps(session, actor)
            logger.info(
                "knowledge_gap_detection_task_completed",
                organization_id=organization_id,
                report_count=len(reports),
            )
        except Exception as exc:
            attempt = ctx["job_try"]
            # Phase 6.2: full jitter -- see `app.ingestion.workers.tasks.
            # _schedule_retry`'s identical comment for why.
            defer_seconds = full_jitter_backoff_seconds(attempt, cap=_MAX_BACKOFF_SECONDS)
            logger.warning(
                "knowledge_gap_detection_task_retry_scheduled",
                organization_id=organization_id,
                attempt=attempt,
                defer_seconds=defer_seconds,
                error=str(exc),
            )
            raise Retry(defer=defer_seconds) from exc
    finally:
        structlog.contextvars.clear_contextvars()


async def scheduled_knowledge_gap_scan(ctx: dict) -> None:
    """Periodic scan (the cron entry point): enqueues one
    `run_knowledge_gap_detection_task` job per organization in the system.

    Enqueues rather than running each organization's detection inline, for
    the exact reason `app.ingestion.workers.tasks.scheduled_reconciliation`
    already enqueues per-connector rather than syncing inline: one
    organization's slower detection pass (larger `agent_executions` volume,
    more LLM topic-synthesis calls) must not block this scan from reaching
    every other organization, and each enqueued job gets its own
    independent retry/backoff.
    """
    async with session_scope() as session:
        organizations = await tenancy_service.list_organizations(session)

    redis = ctx["redis"]
    for organization in organizations:
        await redis.enqueue_job("run_knowledge_gap_detection_task", str(organization.id))

    logger.info("knowledge_gap_scan_scheduled", organization_count=len(organizations))
