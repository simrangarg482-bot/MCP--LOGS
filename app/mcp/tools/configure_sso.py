"""MCP tool: configure_sso (part of the integration-gaps pass that closed
core/tenancy's previously-missing REST/MCP surface).

Lets an MCP client configure the caller's own organization's SSO provider
for the first time -- the same operation `POST /organizations/
{organization_id}/sso/configure` exposes over REST (`app.api.routers.
tenancy.admin_router`). `core.tenancy.service.configure_sso` itself enforces
`tenancy:manage`, raises ConflictError if SSO is already configured (this is
not an upsert), and records the audit event; no extra logic added here.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import service as tenancy_service
from app.core.tenancy.schemas import SSOConfigurationCreate
from app.mcp.dispatch import run_mcp_tool
from app.mcp.servers.server import extract_bearer_token, mcp_server
from app.shared.schemas import Identity


@mcp_server.tool()
async def configure_sso(
    provider: str,
    issuer_url: str,
    client_id: str,
    client_secret_ref: str,
    ctx: Context,
    protocol: str = "oidc",
) -> dict[str, Any]:
    """`{provider: "entra_id"|"okta"|"auth0"|"google_workspace", issuer_url: str,
    client_id: str, client_secret_ref: str, protocol?: "oidc"|"saml"}` ->
    `SSOConfiguration` (serialized). Requires `tenancy:manage`. An invalid
    `provider`/`protocol` value surfaces as a Pydantic validation error when
    building the request schema, the same rough edge already disclosed for
    `search_recent_changes`'s `since` parsing -- see that tool's docstring.
    """
    raw_token = extract_bearer_token(ctx)

    async def handler(session: AsyncSession, identity: Identity) -> dict[str, Any]:
        data = SSOConfigurationCreate(
            provider=provider,
            protocol=protocol,
            issuer_url=issuer_url,
            client_id=client_id,
            client_secret_ref=client_secret_ref,
        )
        sso_config = await tenancy_service.configure_sso(
            session, identity, identity.organization_id, data
        )
        return sso_config.model_dump(mode="json")

    return await run_mcp_tool(
        tool_name="configure_sso",
        raw_token=raw_token,
        request_summary={"provider": provider, "issuer_url": issuer_url},
        handler=handler,
    )
