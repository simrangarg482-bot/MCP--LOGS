"""Public interface for core/users -- identity resolution and authorization.
 
Owned by: core/users. This module is the authority on who a user is and what
they may do. It resolves persisted role assignments into an `Identity`
(consumed everywhere) and provides `authorize()` / `require_permission()` --
the checks that make REST and MCP access control identical (ARCHITECTURE.md
section 6; API_DESIGN.md section 2).
 
Design notes:
  - `resolve_identity` is the ONLY sanctioned way to build a user `Identity`
    (see the deliberate absence of an `Identity.for_user` constructor): it
    guarantees the permission set is actually loaded, so an identity can never
    silently carry empty/incorrect permissions.
  - `authorize` is intentionally pure (no session): once an identity is
    resolved, every permission check is an in-memory set-membership test. This
    is what lets the same identity be checked cheaply many times per request.
"""
 
from __future__ import annotations
 
import uuid
 
from sqlalchemy.ext.asyncio import AsyncSession
 
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.users import repository
from app.core.users.schemas import UserProfile
from app.shared.config.logging import get_logger
from app.shared.schemas import ActorKind, Identity
 
logger = get_logger(__name__)
 
 
async def resolve_identity(session: AsyncSession, user_id: uuid.UUID) -> Identity:
    """Build a fully-populated `Identity` for a user.
 
    Loads the user row plus their role names and flattened permission codes,
    in a small fixed number of indexed queries. Raises:
      - NotFoundError if no such user exists.
      - PermissionDeniedError if the user exists but is deactivated (a disabled
        account must never yield a usable identity).
 
    This is the resolver that core/auth calls after it has verified a
    credential/token; auth never reads roles/permissions itself.
    """
    user = await repository.get_by_id(session, user_id)
    if user is None:
        raise NotFoundError(
            "User not found.",
            error_code="user.not_found",
            detail={"user_id": str(user_id)},
        )
    if not user.is_active:
        raise PermissionDeniedError(
            "User account is inactive.",
            error_code="user.inactive",
            detail={"user_id": str(user_id)},
        )
 
    role_names = await repository.get_role_names(session, user_id)
    permission_codes = await repository.get_permission_codes(session, user_id)
 
    return Identity(
        kind=ActorKind.USER,
        subject=str(user.id),
        user_id=user.id,
        display_name=user.display_name,
        roles=tuple(role_names),
        permissions=frozenset(permission_codes),
    )
 
 
async def get_user_profile(session: AsyncSession, user_id: uuid.UUID) -> UserProfile:
    """Return the human-facing profile (user + roles + permissions) for a user.
 
    Backs `GET /auth/me` and user administration. Raises NotFoundError if the
    user does not exist. Unlike `resolve_identity`, this does not reject
    inactive users -- an admin still needs to see and manage them.
    """
    user = await repository.get_by_id(session, user_id)
    if user is None:
        raise NotFoundError(
            "User not found.",
            error_code="user.not_found",
            detail={"user_id": str(user_id)},
        )
 
    role_names = await repository.get_role_names(session, user_id)
    permission_codes = await repository.get_permission_codes(session, user_id)
 
    return UserProfile(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_active=user.is_active,
        roles=tuple(role_names),
        permissions=tuple(sorted(permission_codes)),
        created_at=user.created_at,
        updated_at=user.updated_at,
    )
 
 
def authorize(actor: Identity, permission_code: str) -> bool:
    """Pure authorization check: does `actor` hold `permission_code`?
 
    No database access -- operates on the permission set already resolved onto
    the identity. This is the boolean form (API_DESIGN.md section 2); use
    `require_permission` when a denial should abort the operation.
    """
    return actor.has_permission(permission_code)
 
 
def require_permission(actor: Identity, permission_code: str) -> None:
    """Enforce `authorize`, raising `PermissionDeniedError` on failure.
 
    The enforcing form used at the top of every guarded service operation, so
    a denied action fails uniformly regardless of entry point (REST or MCP).
    """
    if not authorize(actor, permission_code):
        logger.warning(
            "permission_denied",
            actor=actor.audit_tag,
            required_permission=permission_code,
        )
        raise PermissionDeniedError(
            "You do not have permission to perform this action.",
            error_code="permission_denied",
            detail={"required_permission": permission_code},
        )