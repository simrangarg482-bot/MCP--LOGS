"""MCP tool: `generate_postmortem` (API_DESIGN.md section 3).

API_DESIGN.md's table literally says this tool "Wraps `agents.
generate_postmortem`" with output "`Postmortem` (status will be `draft`)".
Taken literally that is inconsistent: `agents.generate_postmortem` returns a
bare `tuple[str, list[ActionItem]]` (root cause text + action items) and
persists nothing (see that function's own docstring) -- there is no
`Postmortem` row to serialize from calling it directly.
`core.incidents.service.trigger_postmortem_generation` is the actual
persistence glue: it calls `agents.generate_postmortem` internally and
creates the real `Postmortem` row the table's stated output type requires.
This tool calls that function instead, exactly mirroring
`app.api.routers.postmortems.trigger_postmortem`'s identical reasoning for
the REST equivalent -- both boundary layers defer to the same core function
rather than duplicating its persistence logic.
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


@mcp_server.tool()
async def generate_postmortem(incident_id: str, ctx: Context) -> dict[str, Any]:
    """`{incident_id: str}` -> `Postmortem` (serialized, `status="draft"`)."""
    raw_token = extract_bearer_token(ctx)
    parsed_incident_id = uuid.UUID(incident_id)

    async def handler(session: AsyncSession, identity: Identity) -> dict[str, Any]:
        postmortem = await incidents_service.trigger_postmortem_generation(
            session, identity, identity.organization_id, parsed_incident_id
        )
        return postmortem.model_dump(mode="json")

    return await run_mcp_tool(
        tool_name="generate_postmortem",
        raw_token=raw_token,
        request_summary={"incident_id": incident_id},
        handler=handler,
    )
