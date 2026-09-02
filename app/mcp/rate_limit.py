"""Per-caller request-rate limiting for MCP tool calls -- the MCP-side
counterpart to `app.api.rate_limit`.

Owned by: app/mcp. Every MCP tool call routes through
`app.mcp.dispatch.run_mcp_tool`, and until this module existed none of them
were rate-limited at all -- so `ask_question` / `investigate_incident` over
MCP had no ceiling, while their REST equivalents (`POST /ask`,
`POST /ask/investigate`) are throttled per user (`app.api.rate_limit`,
20/min and 10/min). ARCHITECTURE.md section 6's "access control is
identical regardless of entry point" applies to abuse ceilings too, not
just permission checks.

Same engine and same disclosed limitation as `app.api.rate_limit`: the
in-process `app.shared.rate_limiter.TokenBucketRateLimiter`. Correct for a
single MCP server process (`scripts/run_mcp_server.py` runs one); multiple
replicas would each enforce an independent budget. A Redis-backed
distributed limiter is the shared production fix for both this and
`app.api.rate_limit` once either service runs more than one replica --
flagged here as the same follow-up that module already flags, not a new
gap introduced by this file.

Not built as FastAPI dependencies the way `app.api.rate_limit` is (MCP has
no FastAPI dependency-injection surface): a single `enforce_rate_limit`
call near the top of `run_mcp_tool`, after the caller's `Identity` is
resolved, is the MCP equivalent of that module's `Depends(...)`.
"""

from __future__ import annotations

from app.core.exceptions import RateLimitedError
from app.shared.rate_limiter import TokenBucketRateLimiter
from app.shared.schemas import Identity

# One shared limiter for every MCP tool in this process; the key (below)
# namespaces per tool and per caller so budgets never collide.
_limiter = TokenBucketRateLimiter()

# Per-minute budget per caller per tool. The agent-backed tools mirror
# their REST counterparts' limits exactly (`app.api.routers.ask`):
# `ask_question` <-> `POST /ask` (20), `investigate_incident` <->
# `POST /ask/investigate` (10), the two searches <-> `POST /ask/search`
# (30). `generate_postmortem` has no REST rate limit today, but it is an
# LLM-heavy multi-call pipeline, so it gets the same tight budget as
# `investigate_incident` rather than the default. Everything else is a
# cheap single-write/single-read `core/` call -- `_DEFAULT_RPM` covers it.
_DEFAULT_RPM = 30.0
_PER_TOOL_RPM: dict[str, float] = {
    "ask_question": 20.0,
    "investigate_incident": 10.0,
    "generate_postmortem": 10.0,
    "search_similar_incidents": 30.0,
    "search_recent_changes": 30.0,
}


def _caller_key(identity: Identity) -> str:
    """The per-caller dimension, matching `app.api.rate_limit.
    rate_limit_by_user`'s own `actor.user_id or actor.subject` fallback --
    a real user is keyed by their id, an agent/service caller (which has no
    `user_id`) by its stable `subject` name.
    """
    return str(identity.user_id) if identity.user_id is not None else identity.subject


async def enforce_rate_limit(tool_name: str, identity: Identity) -> None:
    """Consume one token for `(tool_name, caller)` or raise
    `RateLimitedError` (429 at the MCP boundary -- `run_mcp_tool` already
    maps `EKIPError.status_hint`). Non-blocking: a throttled call is
    rejected promptly, never held open waiting for a token (same
    `try_acquire` choice `app.api.rate_limit` makes for the same reason).
    """
    requests_per_minute = _PER_TOOL_RPM.get(tool_name, _DEFAULT_RPM)
    rate = requests_per_minute / 60.0
    key = f"mcp.{tool_name}:caller:{_caller_key(identity)}"
    # `capacity=requests_per_minute` (not the bucket's rate-based default):
    # the burst allowance should be the full per-minute quota -- see
    # `TokenBucketRateLimiter`'s docstring on why the default would turn
    # "20 per minute" into "1 then one every 3s".
    if not await _limiter.try_acquire(key, rate, capacity=requests_per_minute):
        raise RateLimitedError(
            "Too many MCP requests for this tool. Please wait before trying again.",
            error_code="rate_limited.mcp",
            detail={"tool": tool_name},
        )
