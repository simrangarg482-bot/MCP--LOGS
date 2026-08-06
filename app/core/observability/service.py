"""Public interface for core/observability -- `record_mcp_request` (the
original write path) plus, as of Milestone 10, `get_mcp_dashboard` (a read
path over the same table). Exists solely so `mcp/` (which cannot import
`app.database` at all -- see `core.observability.schemas`'s module
docstring) has a `core`-side function to call for its own request logging,
the same "call the owning layer's service.py, never its repository.py
directly" convention every other cross-module call in this codebase follows.

`record_mcp_request` has no `require_permission` gate: this is internal
observability bookkeeping triggered on every MCP tool call regardless of
outcome, not a user-initiated business mutation -- the same reasoning
`agents.repository.insert_agent_execution` has no permission gate of its
own either. `get_mcp_dashboard` (new) IS gated, since unlike the write path
it has a real caller-facing surface (a REST endpoint) that should not be
open to just anyone with a valid session.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.observability import repository
from app.core.observability.schemas import McpRequestLog, McpToolStats
from app.core.users.service import require_permission
from app.shared.schemas import Identity

_OBSERVABILITY_READ_PERMISSION = "observability:read"


async def record_mcp_request(
    session: AsyncSession,
    *,
    tool_name: str,
    identity: str,
    request_summary: dict | None,
    status_code: int | None,
    latency_ms: int | None,
) -> McpRequestLog:
    """Log one completed MCP tool call to `mcp_requests`. See
    `app.mcp.dispatch.run_mcp_tool`, the sole caller -- every MCP tool call
    logs exactly once, after it completes, successfully or not.
    """
    row = await repository.insert_mcp_request(
        session,
        tool_name=tool_name,
        identity=identity,
        request_summary=request_summary,
        status_code=status_code,
        latency_ms=latency_ms,
    )
    return McpRequestLog.model_validate(row)


async def get_mcp_dashboard(
    session: AsyncSession, actor: Identity, *, since: datetime | None = None
) -> list[McpToolStats]:
    """Per-tool MCP latency/error aggregate, optionally windowed by `since`
    (Milestone 10's "MCP latency metrics" dashboard requirement,
    PROJECT_PLAN.md section 10).

    Gated by `observability:read`, but deliberately NOT organization-scoped:
    `mcp_requests` carries no `organization_id` at all (see that model's own
    docstring on why), so there is no per-tenant view to restrict this
    to -- every caller with the permission sees the same platform-wide
    aggregate. `actor` is required anyway (not an unauthenticated read) so
    this stays consistent with "every operation takes an actor," even though
    that actor's own organization plays no filtering role here.
    """
    require_permission(actor, _OBSERVABILITY_READ_PERMISSION)

    rows = await repository.get_mcp_tool_stats(session, since=since)
    return [
        McpToolStats(
            tool_name=row.tool_name,
            request_count=row.request_count,
            error_count=int(row.error_count or 0),
            avg_latency_ms=float(row.avg_latency_ms) if row.avg_latency_ms is not None else None,
            max_latency_ms=row.max_latency_ms,
        )
        for row in rows
    ]
