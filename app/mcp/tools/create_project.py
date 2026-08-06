"""MCP tool: create_project (part of the integration-gaps pass that closed
core/tenancy's previously-missing REST/MCP surface).

Lets an MCP client (an AI agent acting on an admin's behalf) create a new
project within the caller's own organization -- the same operation
`POST /organizations/{organization_id}/projects` exposes over REST
(`app.api.routers.tenancy.admin_router`). `core.tenancy.service.
create_project` itself enforces `tenancy:manage` and records the audit
event; this file adds no logic beyond the "validate input -> resolve
Identity -> call core -> translate result" shape ARCHITECTURE.md section 6
requires of every tool handler, matching `propose_runbook_update.py`.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import service as tenancy_service
from app.core.tenancy.schemas import ProjectCreate
from app.mcp.dispatch import run_mcp_tool
from app.mcp.servers.server import extract_bearer_token, mcp_server
from app.shared.schemas import Identity


@mcp_server.tool()
async def create_project(name: str, ctx: Context, is_default: bool = False) -> dict[str, Any]:
    """`{name: str, is_default?: bool}` -> `Project` (serialized), created
    within the caller's own organization. Requires `tenancy:manage`.
    """
    raw_token = extract_bearer_token(ctx)

    async def handler(session: AsyncSession, identity: Identity) -> dict[str, Any]:
        data = ProjectCreate(name=name, is_default=is_default)
        project = await tenancy_service.create_project(
            session, identity, identity.organization_id, data
        )
        return project.model_dump(mode="json")

    return await run_mcp_tool(
        tool_name="create_project",
        raw_token=raw_token,
        request_summary={"name": name, "is_default": is_default},
        handler=handler,
    )
