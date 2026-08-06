"""EKIP's REST API layer.

Owned by: app/api. Per ARCHITECTURE.md section 6 and API_DESIGN.md's
"Design conventions": REST and MCP (app/mcp) are thin, parallel transport
wrappers around the same core/agents Pydantic-typed internal interfaces --
neither is layered on top of the other, and `Identity` is threaded through
every call so access control is identical regardless of entry point.

Unlike app/mcp, app/api has no import-linter restriction against importing
app.database directly: `app.database.session.get_db_session` is already
shaped as a FastAPI dependency (commit-on-success / rollback-on-exception /
always-close), specifically so this layer can `Depends(get_db_session)`
with zero new session-handling code. app/api may not import app.mcp or
app.ingestion internals directly, mirroring the same rule already applied
to app/agents and app/core (see pyproject.toml's import-linter contracts).
"""

from __future__ import annotations
