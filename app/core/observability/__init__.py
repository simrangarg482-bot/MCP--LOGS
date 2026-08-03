"""core/observability -- write access to `mcp_requests` on mcp/'s behalf.

Owns the `mcp_requests` table (DATABASE_DESIGN.md: "mcp/ -- owned tables"),
even though the table is conceptually mcp-motivated -- see
`app.database.models.mcp_models.McpRequest`'s own docstring for why write
access lives here instead: `app.mcp` cannot import `app.database` at all
(import-linter contract), so this small core submodule exists purely to
give mcp/ a `core`-side function to call for its own request logging.

Callers use `from app.core.observability.service import record_mcp_request`;
this package intentionally exposes nothing at import time.
"""
