"""Deliberately empty.

`mcp/` has no repository.py: `pyproject.toml`'s import-linter contract
forbids `app.mcp` from importing `app.database` in any form, including for
`mcp_requests` (the table DATABASE_DESIGN.md conceptually assigns to mcp/'s
concerns). Logging an MCP request is done via
`app.core.observability.service.record_mcp_request` instead -- see
`app.mcp.dispatch.run_mcp_tool` (the sole caller) and
`app.core.observability`'s module docstring for the full reasoning.

This file is kept (rather than left entirely unwritten) so anyone looking
for "mcp/'s repository.py" finds an explanation here instead of nothing.
"""
