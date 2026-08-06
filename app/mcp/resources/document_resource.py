"""MCP resource: `document://{id}` (API_DESIGN.md section 3).

Previously unbuilt: reading a `Document` had no core-owned function to call
into (only `app.ingestion.repository.get_document_by_id`, off-limits to
`app.mcp`). `app.core.knowledge.service.get_document` (added alongside this
file) closes that gap -- it already implements the exact access rule this
resource's contract specifies: "published documents only, unless the
requesting identity has knowledge:review permission."

Same FastMCP-version caveat as `incident_resource.py`: the exact
`@mcp_server.resource(...)` decorator signature could not be confirmed
against the installed `mcp` package (see that module's docstring).
"""

from __future__ import annotations

import uuid
from typing import Any

from mcp.server.mcpserver import Context
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.knowledge import service as knowledge_service
from app.mcp.dispatch import run_mcp_tool
from app.mcp.servers.server import extract_bearer_token, mcp_server
from app.shared.schemas import Identity


@mcp_server.resource("document://{document_id}")
async def get_document_resource(document_id: str, ctx: Context) -> dict[str, Any]:
    """Resolve `document://{document_id}` to `core.knowledge.get_document`."""
    raw_token = extract_bearer_token(ctx)
    parsed_document_id = uuid.UUID(document_id)

    async def handler(session: AsyncSession, identity: Identity) -> dict[str, Any]:
        document = await knowledge_service.get_document(
            session, identity, identity.organization_id, parsed_document_id
        )
        return document.model_dump(mode="json")

    return await run_mcp_tool(
        tool_name="resource:document",
        raw_token=raw_token,
        request_summary={"document_id": document_id},
        handler=handler,
    )
