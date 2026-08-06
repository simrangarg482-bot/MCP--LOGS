"""EKIP MCP server process entrypoint (PROJECT_PLAN.md section 9.6 /
section 7.2). Run as its own process:

    python scripts/run_mcp_server.py

Lives outside `app/mcp/` on purpose: `pyproject.toml`'s import-linter
contract forbids anything under `app.mcp` from importing `app.database` in
any form, but *something* has to open the real database sessions every MCP
tool call needs (identity resolution, the business call itself, and request
logging). This script is that something -- it wires the real
`app.database.session.session_scope` into `app.mcp.servers.server.
session_factory`, and the real `app.database.session.set_tenant_context`
(Milestone 10's RLS backstop) into `app.mcp.servers.server.
set_tenant_context`, once, at startup, then hands control to the FastMCP
server. See `app.mcp.servers.server`'s module docstring for the full
reasoning behind this dependency-inversion split.

**Verify `transport="streamable-http"` against the installed `mcp` package
before running this.** That string is the MCP spec's current canonical name
for this transport, and the value this project's own transport decision
(recorded in `app.mcp.servers.server`'s module docstring) targets -- but
this project's sandbox could not execute Python to confirm `FastMCP.run()`
literally accepts that exact string in the pinned `mcp>=1.0` version (some
FastMCP-derived packages instead use `"http"`). Check
`mcp.server.fastmcp.FastMCP.run`'s actual signature once this runs against
a real install, and adjust this one call if it differs.
"""

from __future__ import annotations

from app.database.session import session_scope, set_tenant_context
from app.mcp.servers import main as mcp_assembly
from app.mcp.servers import server as server_module
from app.shared.config.logging import configure_logging

configure_logging()

# Triggers every registered tool/resource/prompt module's import (see
# `mcp_assembly`'s own docstring) purely for that side effect.
_ = mcp_assembly.mcp_server

server_module.session_factory = session_scope
server_module.set_tenant_context = set_tenant_context

if __name__ == "__main__":
    server_module.mcp_server.run(transport="streamable-http", port=8001)
