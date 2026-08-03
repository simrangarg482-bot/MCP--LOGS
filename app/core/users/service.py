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
  - Multi-tenancy (PROJECT_PLAN.md section 3.5-3.6): every identity is
    resolved *within* one organization, so `resolve_identity` and
    `get_user_profile` both now require `organization_id` -- there is no
    "resolve a user" operation that isn't also "resolve them for this
    organization." `authorize`/`require_permission` additionally accept an
    optional `project_id` for the finer-grained project-scoped check; omitting
    it preserves the original, cheaper org-level-only check.
  - Project-level permission *resolution* (populating
    `Identity.project_permissions` from `project_memberships`) is not wired up
    yet -- that is a separate, not-yet-built piece of core/users (there is no
    project-membership read path here today). `has_permission` already
    supports a `project_id` argument so that follow-up is additive once it
    lands, not another breaking change to this module's signatures.
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


async def resolve_identity(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> Identity:
    """Build a fully-populated `Identity` for a user, scoped to
    `organization_id`.

    Loads the user row plus their role names and flattened permission codes
    *within that organization* (PROJECT_PLAN.md section 3.5), in a small fixed
    number of indexed queries. Raises:
      - NotFoundError if no such user exists.
      - PermissionDeniedError if the user exists but is deactivated (a
        disabled account must never yield a usable identity).

    A user with no role assignment in `organization_id` resolves successfully
    to an Identity with empty `roles`/`permissions`, rather than raising here:
    every downstream `authorize()` check then fails closed for them, which is
    the safe behavior. Verifying that a user is actually a *member* of
    `organization_id` at all (vs. simply having zero permissions there) is a
    login-time concern for core/auth (PROJECT_PLAN.md section 3.3), not
    something this resolver re-derives.

    This is the resolver that core/auth calls after it has verified a
    credential/token and determined which organization the session is scoped
    to; auth never reads roles/permissions itself.
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

    role_names = await repository.get_role_names(session, user_id, organization_id)
    permission_codes = await repository.get_permission_codes(
        session, user_id, organization_id
    )

    return Identity(
        kind=ActorKind.USER,
        subject=str(user.id),
        organization_id=organization_id,
        user_id=user.id,
        display_name=user.display_name,
        roles=tuple(role_names),
        permissions=frozenset(permission_codes),
    )


async def get_user_profile(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> UserProfile:
    """Return the human-facing profile (user + roles + permissions) for a
    user, scoped to `organization_id`.

    Backs `GET /auth/me` and user administration. Scoped the same way as
    `resolve_identity` and for the same reason: the same person can hold
    different roles in different organizations, so "their profile" only means
    something relative to one of them. Raises NotFoundError if the user does
    not exist. Unlike `resolve_identity`, this does not reject inactive users
    -- an admin still needs to see and manage them.
    """
    user = await repository.get_by_id(session, user_id)
    if user is None:
        raise NotFoundError(
            "User not found.",
            error_code="user.not_found",
            detail={"user_id": str(user_id)},
        )

    role_names = await repository.get_role_names(session, user_id, organization_id)
    permission_codes = await repository.get_permission_codes(
        session, user_id, organization_id
    )

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


async def get_or_create_user(
    session: AsyncSession, *, email: str, display_name: str
) -> uuid.UUID:
    """Resolve `email` to a user, creating one if none exists yet.

    Called only after a provisioning decision has already been made
    elsewhere (core/tenancy's `evaluate_provisioning`) -- this function
    performs no authorization/policy check of its own.

    `User` is a single global person record, not scoped to one company
    (see `core_models.py`'s `User` docstring): "does this email already have
    an account" is checked globally, not per-organization, so the same
    person logging into a *second* organization for the first time reuses
    their existing account rather than creating a duplicate -- consistent
    with the existing design that lets one person hold different roles in
    different companies via `UserRole`.
    """
    existing = await repository.get_by_email(session, email)
    if existing is not None:
        return existing.id

    created = await repository.insert_user(session, email=email, display_name=display_name)
    logger.info("user_provisioned", user_id=str(created.id), email=email)
    return created.id


async def assign_role(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    role_id: uuid.UUID,
) -> None:
    """Grant `role_id` to `user_id` within `organization_id`, idempotently.

    Checks for an existing assignment first rather than relying on the
    database to reject a duplicate composite key, so a caller that runs this
    on every login (e.g. re-affirming a role an active access rule grants)
    never has to handle an integrity error for the common "already has this
    role" case.

    Takes no `actor`/permission check: like `get_or_create_user`, this is
    only meant to be called once a decision to grant this role has already
    been made elsewhere (SSO provisioning today; a future admin
    role-management endpoint would need its own `require_permission` check at
    its own boundary, not one added retroactively here).
    """
    existing = await repository.get_user_role(session, user_id, organization_id, role_id)
    if existing is not None:
        return

    await repository.insert_user_role(
        session, user_id=user_id, organization_id=organization_id, role_id=role_id
    )
    logger.info(
        "role_assigned",
        user_id=str(user_id),
        organization_id=str(organization_id),
        role_id=str(role_id),
    )


def authorize(
    actor: Identity, permission_code: str, project_id: uuid.UUID | None = None
) -> bool:
    """Pure authorization check: does `actor` hold `permission_code`?

    No database access -- operates on the permission set(s) already resolved
    onto the identity. With no `project_id`, checks the org-level set only
    (identical behavior to before this migration). With a `project_id`, checks
    that project's override if one exists, else falls back to the org-level
    set (PROJECT_PLAN.md section 3.6). This is the boolean form (API_DESIGN.md
    section 2); use `require_permission` when a denial should abort the
    operation.
    """
    return actor.has_permission(permission_code, project_id=project_id)


def require_permission(
    actor: Identity, permission_code: str, project_id: uuid.UUID | None = None
) -> None:
    """Enforce `authorize`, raising `PermissionDeniedError` on failure.

    The enforcing form used at the top of every guarded service operation, so
    a denied action fails uniformly regardless of entry point (REST or MCP)
    and regardless of whether the check is org-level or project-scoped.
    """
    if not authorize(actor, permission_code, project_id=project_id):
        logger.warning(
            "permission_denied",
            actor=actor.audit_tag,
            organization_id=str(actor.organization_id),
            project_id=str(project_id) if project_id is not None else None,
            required_permission=permission_code,
        )
        raise PermissionDeniedError(
            "You do not have permission to perform this action.",
            error_code="permission_denied",
            detail={"required_permission": permission_code},
        )
