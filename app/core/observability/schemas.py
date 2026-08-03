"""Pydantic contracts for core/observability.

Owned by: core/observability. `McpRequestLog` is the read-side view of one
`mcp_requests` row (`app.database.models.mcp_models.McpRequest`) -- the same
name-mirrors-the-ORM-row convention `agents.schemas.AgentExecution` already
uses for its own table.

This schema (and the module writing it, `core.observability.service`) lives
under `core/`, not `mcp/`, even though `mcp_requests` is conceptually an
mcp-motivated table (DATABASE_DESIGN.md lists it under "mcp/ -- owned
tables") -- see `app.database.models.mcp_models.McpRequest`'s own docstring
for why: `pyproject.toml`'s import-linter contract forbids `app.mcp` from
importing `app.database` in any form, including for its own logging table,
so *something* core-side has to hold write access on mcp/'s behalf. This
mirrors `core.incidents.service.record_investigation_result` in reverse:
that function is core/incidents writing agents-motivated content into its
own table; this module is core writing mcp-motivated content into a table
mcp/ conceptually owns but cannot touch directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class McpRequestLog(BaseModel):
    """One row of `mcp_requests`, as read back after
    `service.record_mcp_request`."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tool_name: str
    identity: str
    request_summary: dict | None
    status_code: int | None
    latency_ms: int | None
    occurred_at: datetime
