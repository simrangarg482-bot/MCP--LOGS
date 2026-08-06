"""MCP tool: create_access_rule (part of the integration-gaps pass that
closed core/tenancy's previously-missing REST/MCP surface).

Lets an MCP client create a domain/group auto-join rule for the caller's own
organization -- the same operation `POST /organizations/{organization_id}/
access-rules` exposes over REST (`app.api.routers.tenancy.admin_router`).
`core.tenancy.service.create_access_rule` itself enforces `tenancy:manage`,
resolves `grants_role` to a role id (raising ValidationError for an unknown
role name), and records the audit event; no extra logic added here.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import service as tenancy_service
from app.core.tenancy.schemas import AccessRuleCreate
from app.mcp.dispatch import run_mcp_tool
from app.mcp.servers.server import extract_bearer_token, mcp_server
from app.shared.schemas import Identity


@mcp_server.tool()
async def create_access_rule(
    rule_type: str, value: str, grants_role: str, ctx: Context, is_active: bool = True
) -> dict[str, Any]:
    """`{rule_type: "domain"|"group", value: str, grants_role: str, is_active?: bool}`
    -> `AccessRule` (serialized). `value` is a domain (e.g. `"acme.com"`) for
    `rule_type="domain"`, or a group name for `rule_type="group"`. Requires
    `tenancy:manage`.
    """
    raw_token = extract_bearer_token(ctx)

    async def handler(session: AsyncSession, identity: Identity) -> dict[str, Any]:
        data = AccessRuleCreate(
            rule_type=rule_type, value=value, grants_role=grants_role, is_active=is_active
        )
        access_rule = await tenancy_service.create_access_rule(
            session, identity, identity.organization_id, data
        )
        return access_rule.model_dump(mode="json")

    return await run_mcp_tool(
        tool_name="create_access_rule",
        raw_token=raw_token,
        request_summary={"rule_type": rule_type, "value": value, "grants_role": grants_role},
        handler=handler,
    )
