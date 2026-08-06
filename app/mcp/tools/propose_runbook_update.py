"""MCP tool: `propose_runbook_update` (API_DESIGN.md section 3).

Previously unbuilt: this tool's documented behavior ("creates a `documents`
row with `status=proposed`") had no core-owned function to call --
`app.core.knowledge.service.propose_document` (added alongside this file)
closes that gap. See that module's docstring for the full design, including
why a `documents.content` field is stored as `document_metadata` rather
than a real column.
"""

from __future__ import annotations

import uuid
from typing import Any

from mcp.server.mcpserver import Context
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.knowledge import service as knowledge_service
from app.core.knowledge.schemas import DocumentProposalCreate
from app.mcp.dispatch import run_mcp_tool
from app.mcp.servers.server import extract_bearer_token, mcp_server
from app.shared.schemas import Identity


@mcp_server.tool()
async def propose_runbook_update(
    title: str, content: str, ctx: Context, source_incident_id: str | None = None
) -> dict[str, Any]:
    """`{title: str, content: str, source_incident_id?: str}` -> `Document`
    (serialized, `status="proposed"`).
    """
    raw_token = extract_bearer_token(ctx)
    parsed_source_incident_id = uuid.UUID(source_incident_id) if source_incident_id else None

    async def handler(session: AsyncSession, identity: Identity) -> dict[str, Any]:
        data = DocumentProposalCreate(
            title=title, content=content, source_incident_id=parsed_source_incident_id
        )
        document = await knowledge_service.propose_document(
            session, identity, identity.organization_id, data
        )
        return document.model_dump(mode="json")

    return await run_mcp_tool(
        tool_name="propose_runbook_update",
        raw_token=raw_token,
        request_summary={"title": title, "source_incident_id": source_incident_id},
        handler=handler,
    )
