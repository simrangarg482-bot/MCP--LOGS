"""MCP tool: create_invitation (part of the integration-gaps pass that
closed core/tenancy's previously-missing REST/MCP surface).

Lets an MCP client invite an email address to join the caller's own
organization -- the same operation `POST /organizations/{organization_id}/
invitations` exposes over REST (`app.api.routers.tenancy.admin_router`).
`core.tenancy.service.create_invitation` itself enforces `tenancy:manage`,
requires a `USER`-kind actor (only a human may send an invitation --
`invitations.invited_by` references a real `users` row), and records the
audit event; no extra logic added here, matching every other tool file in
this package.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from mcp.server.mcpserver import Context
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import service as tenancy_service
from app.core.tenancy.schemas import InvitationCreate
from app.mcp.dispatch import run_mcp_tool
from app.mcp.servers.server import extract_bearer_token, mcp_server
from app.shared.schemas import Identity


@mcp_server.tool()
async def create_invitation(
    email: str, grants_role: str, ctx: Context, expires_at: str | None = None
) -> dict[str, Any]:
    """`{email: str, grants_role: str, expires_at?: str (ISO-8601)}` ->
    `Invitation` (serialized). Omitting `expires_at` uses `core.tenancy.
    service`'s own default lifetime (14 days). Requires `tenancy:manage`.
    """
    raw_token = extract_bearer_token(ctx)
    parsed_expires_at = datetime.fromisoformat(expires_at) if expires_at else None

    async def handler(session: AsyncSession, identity: Identity) -> dict[str, Any]:
        data = InvitationCreate(email=email, grants_role=grants_role, expires_at=parsed_expires_at)
        invitation = await tenancy_service.create_invitation(
            session, identity, identity.organization_id, data
        )
        return invitation.model_dump(mode="json")

    return await run_mcp_tool(
        tool_name="create_invitation",
        raw_token=raw_token,
        request_summary={"email": email, "grants_role": grants_role},
        handler=handler,
    )
