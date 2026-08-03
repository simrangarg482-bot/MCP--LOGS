"""MCP authentication boundary (PROJECT_PLAN.md section 7.4 / ARCHITECTURE.md
section 6): turns a raw bearer token, as presented by an MCP client, into a
fully-populated `Identity`.

This is not an MCP-specific reimplementation of token verification -- it is
a two-call composition of the exact same functions `core/auth` and
`core/users` already expose for this purpose:
`core.auth.service.verify_access_token(token) -> TokenClaims` (a pure,
DB-free JWT decode -- "whose token is this, and is it validly signed and
unexpired") followed by `core.users.service.resolve_identity(session,
user_id, organization_id) -> Identity` (the DB-backed step that loads roles/
permissions). `verify_access_token`'s own docstring already names this
exact split: "turning that into a full Identity ... is
core.users.service.resolve_identity's job, called separately by whatever
boundary layer (REST or MCP) verified this token." mcp/ is that second
boundary layer -- there is only one way to turn a token into an `Identity`
in this codebase; this module does not add a second one.

Resolved per tool call, not cached for the lifetime of a connection: the
streamable-HTTP transport this server targets is a hosted, multi-tenant
endpoint (not a per-user local subprocess), so there is no single
"connection" a client keeps open for its whole session the way a stdio
transport would have -- each HTTP request carries its own bearer token, and
`verify_access_token` (pure JWT decode) plus `resolve_identity` (a small,
fixed number of indexed queries) are both cheap enough to redo per call
without a caching layer.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import service as auth_service
from app.core.users import service as users_service
from app.shared.schemas import Identity


async def resolve_mcp_identity(session: AsyncSession, raw_token: str) -> Identity:
    """Resolve a raw bearer token into a fully-populated `Identity`.

    Raises `app.core.exceptions.PermissionDeniedError` (an `EKIPError`
    subclass) for a missing, malformed, or expired token, or
    `NotFoundError`/`PermissionDeniedError` (also `EKIPError` subclasses) if
    the token's user no longer exists or has been deactivated -- all three
    propagate unchanged to the calling tool handler, which maps them to an
    MCP error response the same way every other module's callers let
    `EKIPError` subclasses propagate to their own boundary.
    """
    claims = auth_service.verify_access_token(raw_token)
    return await users_service.resolve_identity(session, claims.user_id, claims.organization_id)
