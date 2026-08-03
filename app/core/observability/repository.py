"""Persistence for core/observability -- `mcp_requests` only
(DATABASE_DESIGN.md's "mcp/ -- owned tables"; see `core.observability.
schemas`'s module docstring for why this write access lives under core/
rather than mcp/). Pure data access, same discipline as every other
repository.py in this codebase: one statement per function, ORM rows in/out,
no business rules, no ORM->Pydantic mapping.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.mcp_models import McpRequest


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
