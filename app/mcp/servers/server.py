"""The EKIP MCP server instance (PROJECT_PLAN.md section 9.6 / section 7.2).

One `FastMCP` server, targeting the **streamable-HTTP** transport -- a
hosted, multi-tenant endpoint serving every organization's MCP traffic
through one running process, not a per-user local stdio subprocess. This
was a genuinely undecided choice in the docs (section 7.2's diagram lists
"stdio or HTTP+SSE" without picking one); streamable-HTTP was chosen because
it fits EKIP's actual deployment model -- many organizations, many users,
one hosted service, each request carrying its own bearer token -- the same
reasoning `mcp.auth`'s module docstring gives for resolving `Identity` per
call rather than once per long-lived connection.

Tool/resource/prompt handlers live in `mcp/tools/` and `mcp/resources/`,
each importing `mcp_server` from this module to register themselves via
`@mcp_server.tool()` / `@mcp_server.resource(...)` / `@mcp_server.prompt()`.
This module owns only the server instance and the bearer-token extraction
glue -- not any tool's business logic (ARCHITECTURE.md section 6: "no
business logic" in mcp/, enforced in practice by `mcp.dispatch.run_mcp_tool`,
which every handler calls into).

**`session_factory` -- why it lives here, as an injected, initially-`None`
attribute:** `pyproject.toml`'s import-linter contract forbids `app.mcp`
from importing `app.database` in any form (confirmed as the intended,
literal reading, not just a loose docstring aspiration -- see
`app.core.observability`'s module docstring), which means nothing under
`app.mcp` -- including this module and `app.mcp.dispatch` -- may import
`app.database.session.session_scope` to open its own DB session, even
though every `core`/`agents` function `mcp.dispatch.run_mcp_tool` calls into
needs one passed in. The fix is dependency inversion: this module declares
the *shape* it needs (a zero-argument callable returning an async context
manager yielding an `AsyncSession` -- `AsyncSession` is a third-party
SQLAlchemy type, not `app.database`, so typing against it here is fine)
without importing a concrete implementation. The actual process entrypoint,
`scripts/run_mcp_server.py` -- which lives *outside* `app.mcp` and is
therefore free to import `app.database.session` -- sets
`server.session_factory = session_scope` once at startup, before serving
any request. `app.mcp.dispatch.run_mcp_tool` reads this module's
`session_factory` attribute at call time (not via a top-level `from ...
import session_factory`, which would freeze on the pre-startup `None`).

**Verify against the installed `mcp` package before deploying.** This
project's sandbox could not execute Python during development (`pip show
mcp` and a live import both failed -- no disk space available to start the
isolated environment), so the exact API surface below could not be
confirmed against the actually-installed version:
  - `FastMCP(name=...)` and the `@mcp_server.tool()` /
    `@mcp_server.resource(...)` / `@mcp_server.prompt()` decorators are the
    long-stable core of the FastMCP API and are very likely correct as-is.
  - `extract_bearer_token`'s reach into `ctx.request_context.request.headers`
    is the one piece most likely to need adjusting: newer FastMCP-derived
    packages expose a `get_access_token()` / `get_http_headers()`
    dependency-injection helper instead of (or in addition to) raw
    `Context` attribute access, and some versions ship a built-in
    `TokenVerifier`-based auth provider (`mcp.server.auth`, passed as
    `FastMCP(..., auth=verifier)`) that would let the MCP layer itself
    reject invalid tokens before a tool handler runs at all -- if the
    installed version has that, prefer it over this manual header read and
    delete this function; if it doesn't, confirm this attribute path
    against `mcp.server.fastmcp.Context`'s real source before relying on it.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from mcp.server.fastmcp import Context, FastMCP
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PermissionDeniedError

mcp_server = FastMCP(name="ekip")

# Set once, at process startup, by `scripts/run_mcp_server.py` -- see this
# module's docstring. Left `None` until then so an accidental tool call
# before startup wiring runs fails loudly (`run_mcp_tool` below) rather than
# silently doing nothing.
session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] | None = None


def extract_bearer_token(ctx: Context) -> str:
    """Pull the caller's bearer access token out of the current MCP
    request's `Authorization` header. See this module's docstring for the
    version-verification caveat on this specific function.
    """
    request = getattr(ctx.request_context, "request", None)
    if request is None:
        raise PermissionDeniedError(
            "MCP request has no HTTP context to read a bearer token from.",
            error_code="mcp.no_transport_context",
        )

    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise PermissionDeniedError(
            "Missing or malformed Authorization header.",
            error_code="mcp.missing_token",
        )
    return header[len("bearer ") :].strip()
