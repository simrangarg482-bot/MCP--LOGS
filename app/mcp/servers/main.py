"""Assembles the EKIP MCP server's tool surface (PROJECT_PLAN.md section 9.6
/ section 7.2) -- imports every `mcp/tools/`, `mcp/resources/` module so
their `@mcp_server.tool()` / `.resource(...)` / `.prompt()` decorators
actually register (the same "import purely for its registration side
effect" pattern `app.database.migrations.base`'s per-model-module imports
already rely on for Alembic autogenerate), and exposes the assembled
`mcp_server` for something else to run.

Deliberately does **not** run the server or wire up `session_factory`
itself: both of those need `app.database.session`, which nothing under
`app.mcp` may import (see `app.mcp.servers.server`'s module docstring).
`scripts/run_mcp_server.py` -- outside `app.mcp` -- is the actual process
entrypoint: it imports this module (triggering every tool's registration),
sets `server.session_factory`, and calls `mcp_server.run(...)`.

Add a new import line here as each tool/resource/prompt module lands (tasks
#27-31); none exist yet.
"""

from __future__ import annotations

from app.mcp.servers.server import mcp_server

__all__ = ["mcp_server"]

# from app.mcp.tools import ask_question  # noqa: F401
# from app.mcp.tools import investigate_incident  # noqa: F401
# from app.mcp.tools import generate_postmortem  # noqa: F401
# from app.mcp.tools import search_similar_incidents  # noqa: F401
# from app.mcp.tools import search_recent_changes  # noqa: F401
# from app.mcp.tools import propose_runbook_update  # noqa: F401
# from app.mcp.resources import incident_resource  # noqa: F401
# from app.mcp.resources import document_resource  # noqa: F401
