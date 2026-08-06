"""MCP resource: `incident://{id}` (API_DESIGN.md section 3).

Resolves to `core.incidents.service.get_incident`, exactly as the contract
table specifies. Uses `run_mcp_tool` for the same identity-resolution /
session-lifecycle / observability-logging bookkeeping every tool handler
gets (`app.mcp.dispatch.run_mcp_tool`'s own docstring already anticipates
resources doing this: "every MCP tool handler ... (and, eventually,
resources) runs through").

**Verify the exact resource-template decorator signature against the
installed `mcp` package before deploying** -- same caveat
`app.mcp.servers.server`'s module docstring already raises for
`extract_bearer_token`; this project's sandbox could not execute Python
during development to confirm `@mcp_server.resource("incident://{incident_id}")`
is literally how FastMCP maps a URI template segment onto a handler
parameter name in the pinned `mcp>=1.0` version.
"""

from __future__ import annotations

import uuid
from typing import Any

from mcp.server.mcpserver import Context
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.incidents import service as incidents_service
from app.mcp.dispatch import run_mcp_tool
from app.mcp.servers.server import extract_bearer_token, mcp_server
from app.shared.schemas import Identity


@mcp_server.resource("incident://{incident_id}")
async def get_incident_resource(incident_id: str, ctx: Context) -> dict[str, Any]:
    """Resolve `incident://{incident_id}` to `core.get_incident`."""
    raw_token = extract_bearer_token(ctx)
    parsed_incident_id = uuid.UUID(incident_id)

    async def handler(session: AsyncSession, identity: Identity) -> dict[str, Any]:
        incident = await incidents_service.get_incident(
            session, identity, identity.organization_id, parsed_incident_id
        )
        return incident.model_dump(mode="json")

    return await run_mcp_tool(
        tool_name="resource:incident",
        raw_token=raw_token,
        request_summary={"incident_id": incident_id},
        handler=handler,
    )
