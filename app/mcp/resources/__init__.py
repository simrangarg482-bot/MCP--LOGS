"""Milestone 8 MCP resources (API_DESIGN.md section 3: `incident://{id}`,
`document://{id}`).

Owned by: app/mcp. Each module registers exactly one `@mcp_server.resource
(...)` via import side effect, the same registration pattern `app/mcp/
tools/__init__.py` documents for tools -- `app.mcp.servers.main` imports
every module here for that reason.

Both documented resources are now implemented. `document_resource.py` was
initially blocked because no core-owned function read a `Document` row --
closed by `app.core.knowledge.service.get_document`, which already
implements the exact "published documents only, unless the requesting
identity has knowledge:review" access rule this resource's contract
specifies.
"""

from __future__ import annotations
