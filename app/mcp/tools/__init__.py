"""Milestone 8 MCP tool handlers (API_DESIGN.md section 3).

Owned by: app/mcp. Each module registers exactly one `@mcp_server.tool()`
via import side effect -- `app.mcp.servers.main` imports every module here
for that reason (see its own docstring). Every handler's body is,
without exception, ARCHITECTURE.md section 6's rule made literal: validate
input -> resolve Identity (via `app.mcp.dispatch.run_mcp_tool`) -> call a
`core`/`agents` public function -> translate the result into a JSON-
serializable shape. No handler contains business logic beyond that
translation.

All six documented tools (including `propose_runbook_update`) are now
implemented. `propose_runbook_update` was initially blocked because no
core-owned function existed to create a `documents` row with
`status=proposed` -- closed by `app.core.knowledge.service.propose_document`
(see that module's own docstring for the full design, including why it's a
deliberate, flagged extension of `documents`' usual single-writer
convention).
"""

from __future__ import annotations
