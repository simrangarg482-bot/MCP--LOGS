"""Shared plumbing every MCP tool handler in `mcp/tools/` (and, eventually,
`mcp/resources/`) runs through: resolve the caller's `Identity` from their
bearer token, run the handler inside one request-scoped transaction, log the
outcome to `mcp_requests`, and consistently map an `EKIPError` to a status
code. Factored out here so no individual tool handler duplicates this
bookkeeping -- ARCHITECTURE.md section 6 is explicit that "every tool
handler's body is, without exception: validate input -> resolve Identity ->
call core/agents -> translate result," and `run_mcp_tool` is what makes that
literally true in code rather than a convention each handler has to
remember to follow.

Not named in PROJECT_PLAN.md section 10's file tree (`servers/`, `tools/`,
`resources/` only) -- a deliberate, flagged addition, the same kind
`agents/answer/`'s own docstring already precedents: this file's
responsibility (identity resolution + session lifecycle + observability
logging, shared across every tool) doesn't belong inside any single tool
handler, and duplicating it five times across `mcp/tools/*.py` would be far
worse than one extra top-level module.

**No `app.database` import here, deliberately** -- unlike every other
module's equivalent plumbing, this file gets its session from
`app.mcp.servers.server.session_factory` (an injected callable, set at
process startup by `scripts/run_mcp_server.py`, which lives outside
`app.mcp` and is free to import `app.database.session`) rather than calling
`session_scope()` itself. See `app.mcp.servers.server`'s module docstring
for the full reasoning -- `app.mcp` cannot import `app.database` in any
form, confirmed as the intended, literal reading of the import-linter
contract, not just a loose aspiration.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EKIPError
from app.core.observability import service as observability_service
from app.mcp.auth import resolve_mcp_identity
from app.mcp.servers import server as server_module
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity

logger = get_logger(__name__)

T = TypeVar("T")

# `EKIPError.status_hint` already *is* the "future REST/MCP boundary layer"
# mapping `app.core.exceptions.EKIPError`'s own docstring anticipates ("the
# boundary layer needs a stable ... status mapping that does not depend on
# guessing from a built-in type ... so the transport layer stays a thin
# translation with no per-error if isinstance(...) ladder") -- so this
# module reads that attribute directly rather than hand-rolling a second,
# parallel mapping that could drift out of sync with it.
_UNEXPECTED_ERROR_STATUS = 500


class McpServerNotReadyError(RuntimeError):
    """Raised if a tool call arrives before `scripts/run_mcp_server.py` has
    set `app.mcp.servers.server.session_factory` -- a startup-ordering bug,
    not a normal runtime condition, so this is a plain `RuntimeError`
    subclass rather than an `EKIPError` (nothing about the caller's request
    was wrong).
    """


async def run_mcp_tool(
    *,
    tool_name: str,
    raw_token: str,
    request_summary: dict,
    handler: Callable[[AsyncSession, Identity], Awaitable[T]],
) -> T:
    """Run one MCP tool call end-to-end.

    Opens exactly one session (via `app.mcp.servers.server.session_factory`)
    for `resolve_mcp_identity` and `handler` together, so both run inside
    the same transaction -- matching this codebase's universal "session
    passed in, never opened internally" rule (every `core`/`agents`
    function takes a session; something at the boundary has to open one,
    and for MCP, this function is that boundary -- the same role
    `app.database.session.get_db_session` plays for a future REST layer,
    just sourced from an injected factory instead of a direct import; see
    module docstring). `handler` receives that session plus the resolved
    `Identity` and is expected to call straight into a `core`/`agents`
    public function with them -- it should contain no logic beyond that
    single call and translating its result into whatever shape the tool
    handler returns to FastMCP.

    The `mcp_requests` log write happens via `core.observability.service.
    record_mcp_request`, in a **separate** session (again from
    `session_factory`), deliberately, in a `finally` block after the main
    transaction has already committed or rolled back: if `handler` raises
    and its transaction rolls back, the log entry recording *that failure*
    must still persist -- writing it inside the same, now-rolled-back
    transaction would roll the log entry back too, silently losing the one
    observability record that would explain why the call failed. A logging
    failure itself is swallowed (logged locally, not re-raised) -- losing
    one `mcp_requests` row must never turn a real tool-call failure (or a
    real success) into a different, misleading error for the client.

    Re-raises whatever `handler` raised (after logging it) -- translating
    that into an actual MCP protocol error response is the calling tool
    handler's job, since that translation depends on exactly what the
    installed `mcp`/FastMCP version expects a raised exception to look like.
    """
    if server_module.session_factory is None:
        raise McpServerNotReadyError(
            "app.mcp.servers.server.session_factory is unset -- "
            "scripts/run_mcp_server.py must set it before serving requests."
        )
    session_factory = server_module.session_factory

    start = time.monotonic()
    status_code = 200
    identity_tag = "unresolved"
    outcome_error: BaseException | None = None

    try:
        async with session_factory() as session:
            identity = await resolve_mcp_identity(session, raw_token)
            identity_tag = identity.audit_tag
            return await handler(session, identity)
    except EKIPError as exc:
        status_code = exc.status_hint
        outcome_error = exc
        raise
    except Exception as exc:
        status_code = _UNEXPECTED_ERROR_STATUS
        outcome_error = exc
        raise
    finally:
        latency_ms = int((time.monotonic() - start) * 1000)
        try:
            async with session_factory() as log_session:
                await observability_service.record_mcp_request(
                    log_session,
                    tool_name=tool_name,
                    identity=identity_tag,
                    request_summary=request_summary,
                    status_code=status_code,
                    latency_ms=latency_ms,
                )
        except Exception as log_exc:
            logger.warning(
                "mcp_request_log_failed",
                tool_name=tool_name,
                original_error=str(outcome_error) if outcome_error else None,
                logging_error=str(log_exc),
            )
