"""MCP prompts (API_DESIGN.md section 3: `triage-incident`, `draft-postmortem`).

Owned by: app/mcp. Each module registers exactly one `@mcp_server.prompt()`
via import side effect, the same registration pattern `app/mcp/tools/
__init__.py` documents -- `app.mcp.servers.main` imports every module here
for that reason.

Unlike tools/resources, a prompt is pure text: MCP prompts are templates an
MCP client retrieves and sends to its own LLM as conversation-starting
content -- they do not themselves resolve an `Identity`, open a session, or
call any `core`/`agents` function (there is nothing to authenticate or run
yet; the *tool call* the returned text asks the client's LLM to make is
what does that, when the client's LLM actually issues it). Neither module
here uses `app.mcp.dispatch.run_mcp_tool` for this reason -- it would have
nothing to wrap.
"""

from __future__ import annotations
