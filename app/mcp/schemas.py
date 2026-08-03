"""Pydantic contracts local to mcp/ -- not shared cross-module (contrast
`app.shared.schemas`, which holds types produced by one module *for* another
to consume).

Empty for now: `McpRequestLog` (the read-side view of one `mcp_requests`
row) lives in `app.core.observability.schemas` instead, not here -- see that
module's docstring for why (`app.mcp` cannot import `app.database`, and the
ORM row this schema mirrors lives under `app.database.models`).

Reserved for MCP tool-input/output-specific schemas as tasks #27-31 add
tool/resource/prompt handlers, if any handler needs a shape beyond what
`shared.schemas`/`core`/`agents` already provide.
"""
