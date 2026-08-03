"""Public interface for core/observability -- today, just
`record_mcp_request`. Exists solely so `mcp/` (which cannot import
`app.database` at all -- see `core.observability.schemas`'s module
docstring) has a `core`-side function to call for its own request logging,
the same "call the owning layer's service.py, never its repository.py
directly" convention every other cross-module call in this codebase follows.

No `require_permission` gate: this is internal observability bookkeeping
triggered on every MCP tool call regardless of outcome, not a user-initiated
business mutation -- the same reasoning `agents.repository.
insert_agent_execution` has no permission gate of its own either.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.observability import repository
from app.core.observability.schemas import McpRequestLog


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
