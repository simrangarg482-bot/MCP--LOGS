"""Observability dashboard router -- Milestone 10's "agent_executions/
mcp_requests dashboards, latency metrics" requirement (PROJECT_PLAN.md
section 10), the first REST-facing surface either of those tables has ever
had.

Owned by: app/api. Both handlers are thin pass-throughs, same as every
other router here -- `/observability/agents` into `agents.service.
get_agent_execution_stats`, `/observability/mcp` into `core.observability.
service.get_mcp_dashboard`. `since` is an optional query parameter on both,
passed straight through as a `datetime` (FastAPI/Pydantic parses an ISO 8601
query string automatically); omitting it means "all time," matching each
service function's own `since: datetime | None = None` default.

`/observability/mcp` deliberately takes no `organization_id`-shaped
path/query parameter at all, unlike every other router in this package --
see `core.observability.service.get_mcp_dashboard`'s own docstring for why
(`mcp_requests` carries no `organization_id` column; this is a genuinely
platform-wide view, not an oversight in this router).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter

from app.agents import service as agents_service
from app.agents.schemas import AgentExecutionStats
from app.api.deps import CurrentIdentity, DbSession
from app.core.observability import service as observability_service
from app.core.observability.schemas import McpToolStats

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
