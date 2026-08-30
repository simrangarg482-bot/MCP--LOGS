"""Observability dashboard router -- Milestone 10's "agent_executions/
mcp_requests dashboards, latency metrics" requirement (PROJECT_PLAN.md
section 10), the first REST-facing surface either of those tables has ever
had. `/observability/ingestion` (Phase 5.6) fills the equivalent gap for
`ingestion_jobs`, which had no aggregate dashboard at all before this.

Owned by: app/api. All three handlers are thin pass-throughs, same as every
other router here -- `/observability/agents` into `agents.service.
get_agent_execution_stats`, `/observability/mcp` into `core.observability.
service.get_mcp_dashboard`, `/observability/ingestion` into `core.tenancy.
service.get_ingestion_job_stats` (not `app.ingestion` -- `app.api` is
import-linter-forbidden from depending on it; see that function's own
docstring). `since` is an optional query parameter on all three, passed
straight through as a `datetime` (FastAPI/Pydantic parses an ISO 8601 query
string automatically); omitting it means "all time," matching each service
function's own `since: datetime | None = None` default.

`/observability/mcp` deliberately takes no `organization_id`-shaped
path/query parameter at all, unlike every other router in this package --
see `core.observability.service.get_mcp_dashboard`'s own docstring for why
(`mcp_requests` carries no `organization_id` column; this is a genuinely
platform-wide view, not an oversight in this router).
"""

from __future__ import annotations

import time
from datetime import datetime

from fastapi import APIRouter

from app.agents import service as agents_service
from app.agents.schemas import AgentExecutionStats
from app.api.deps import ArqPool, CurrentIdentity, DbSession
from app.core.observability import service as observability_service
from app.core.observability.schemas import McpToolStats
from app.core.tenancy import service as tenancy_service
from app.core.tenancy.schemas import IngestionJobStats, IngestionQueueHealth
from app.core.users.service import require_permission
from app.shared.config.settings import get_settings

router = APIRouter(prefix="/observability", tags=["observability"])


@router.get("/agents", response_model=list[AgentExecutionStats])
async def get_agent_execution_stats(
    actor: CurrentIdentity,
    session: DbSession,
    since: datetime | None = None,
) -> list[AgentExecutionStats]:
    return await agents_service.get_agent_execution_stats(session, actor, since=since)


@router.get("/mcp", response_model=list[McpToolStats])
async def get_mcp_dashboard(
    actor: CurrentIdentity,
    session: DbSession,
    since: datetime | None = None,
) -> list[McpToolStats]:
    return await observability_service.get_mcp_dashboard(session, actor, since=since)


@router.get("/ingestion", response_model=list[IngestionJobStats])
async def get_ingestion_job_stats(
    actor: CurrentIdentity,
    session: DbSession,
    since: datetime | None = None,
) -> list[IngestionJobStats]:
    return await tenancy_service.get_ingestion_job_stats(session, actor, since=since)


@router.get("/ingestion/queue", response_model=IngestionQueueHealth)
async def get_ingestion_queue_health(
    actor: CurrentIdentity,
    arq_pool: ArqPool,
) -> IngestionQueueHealth:
    """Live queue pressure complementing database-backed run history."""
    require_permission(actor, "observability:read")
    queue_name = "arq:queue:ingestion"
    queued_jobs = int(await arq_pool.zcard(queue_name))
    oldest = await arq_pool.zrange(queue_name, 0, 0, withscores=True)
    oldest_age = None
    if oldest:
        _job, score_ms = oldest[0]
        oldest_age = max(0.0, time.time() - (float(score_ms) / 1000))
    return IngestionQueueHealth(
        queued_jobs=queued_jobs,
        oldest_queued_age_seconds=oldest_age,
        worker_max_concurrency=get_settings().ingestion_worker_max_jobs,
    )
