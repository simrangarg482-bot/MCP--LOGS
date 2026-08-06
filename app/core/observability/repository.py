"""Persistence for core/observability -- `mcp_requests` only
(DATABASE_DESIGN.md's "mcp/ -- owned tables"; see `core.observability.
schemas`'s module docstring for why this write access lives under core/
rather than mcp/). Pure data access, same discipline as every other
repository.py in this codebase: one statement per function, ORM rows in/out,
no business rules, no ORM->Pydantic mapping.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.mcp_models import McpRequest

# A request without a recorded status_code (a call that failed before
# `mcp.dispatch.run_mcp_tool` could even map an outcome to one) is counted as
# an error -- absence of a status here means "something went wrong before
# normal completion," not "unknown, ignore it," matching this table's own
# "logged after it completes, successfully or not" guarantee (see
# `insert_mcp_request`'s docstring: every call is logged exactly once).
_ERROR_STATUS_CASE = case(
    (McpRequest.status_code.is_(None), 1),
    (McpRequest.status_code >= 400, 1),
    else_=0,
)


async def get_mcp_tool_stats(
    session: AsyncSession, *, since: datetime | None = None
) -> list[Any]:
    """Aggregate `mcp_requests` by `tool_name`: count, error count, average
    and max latency -- backs the Milestone 10 observability dashboard
    (`core.observability.service.get_mcp_dashboard`).

    Returns raw SQLAlchemy `Row` objects (named-tuple-like, with `.tool_name`
    /`.request_count`/`.error_count`/`.avg_latency_ms`/`.max_latency_ms`
    attributes matching the query's `label()`s) rather than ORM instances --
    an aggregate query has no single `McpRequest` row to map back onto, so
    the usual "return ORM rows, let service.py map to Pydantic" convention
    doesn't apply verbatim; `service.py` builds `McpToolStats` directly from
    these labeled columns instead.
    """
    stmt = select(
        McpRequest.tool_name.label("tool_name"),
        func.count().label("request_count"),
        func.sum(_ERROR_STATUS_CASE).label("error_count"),
        func.avg(McpRequest.latency_ms).label("avg_latency_ms"),
        func.max(McpRequest.latency_ms).label("max_latency_ms"),
    ).group_by(McpRequest.tool_name)
    if since is not None:
        stmt = stmt.where(McpRequest.occurred_at >= since)

    result = await session.execute(stmt)
    return list(result.all())


async def insert_mcp_request(
    session: AsyncSession,
    *,
    tool_name: str,
    identity: str,
    request_summary: dict | None,
    status_code: int | None,
    latency_ms: int | None,
) -> McpRequest:
    """Log one completed MCP tool call. Unlike `agents.repository.
    insert_agent_execution`, this is not a "create running, update later"
    pair -- every field here is already known by the time a tool call has
    finished (successfully or not), so this is the only write this table
    ever needs per call. See `McpRequest`'s own docstring.
    """
    row = McpRequest(
        tool_name=tool_name,
        identity=identity,
        request_summary=request_summary,
        status_code=status_code,
        latency_ms=latency_ms,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row
