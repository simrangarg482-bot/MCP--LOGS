"""Persistence for core/users -- users, roles, permissions.

Owned by: core/users. Pure data access: each function issues one query and
returns ORM rows or plain scalar values; identity assembly and authorization
decisions live in service.py.

The two resolution queries (`get_role_names`, `get_permission_codes`) are the
performance-relevant heart of RBAC: they let the service resolve an identity
in two small indexed queries once per request, so every downstream
`authorize()` is then a pure in-memory set check with no further DB hits.

Multi-tenancy (PROJECT_PLAN.md section 3.5): role assignment is scoped per
organization -- `UserRole`'s primary key is `(user_id, organization_id,
role_id)`, not just `(user_id, role_id)`, because the same person can hold
different roles in different companies. Both resolution queries below filter
by `organization_id` as well as `user_id` accordingly: omitting that filter
would resolve (and leak) a user's roles/permissions across every organization
they belong to, not just the one their session is scoped to.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.core_models import (
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)


async def get_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Fetch a single user by primary key, or None if absent."""
    return await session.get(User, user_id)


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    """Fetch a single user by their unique email, or None if absent.

    Used by core/auth during credential resolution (login looks a user up by
    email before issuing a token).
    """
    stmt = select(User).where(User.email == email)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_role_names(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> Sequence[str]:
    """Return the names of the roles assigned to `user_id` *within
    `organization_id`*.

    Joins `user_roles -> roles`, filtered by both `user_id` and
    `organization_id` -- a role assignment held in a different organization
    must never be returned (PROJECT_PLAN.md section 3.5). Empty sequence if
    the user has no roles in this organization.
    """
    stmt = (
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(
            UserRole.user_id == user_id,
            UserRole.organization_id == organization_id,
        )
        .order_by(Role.name)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def insert_user(session: AsyncSession, *, email: str, display_name: str) -> User:
    """Create a new user row and return it with server defaults populated.

    Called only after a decision to allow provisioning has already been made
    elsewhere (core/tenancy's `evaluate_provisioning`) -- this function
    performs no authorization or policy check of its own; core/users manages
    identity/roles, not who may join an organization.
    """
    row = User(email=email, display_name=display_name)
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_role_by_name(session: AsyncSession, name: str) -> Role | None:
    """Fetch a single role by its unique name, or None if absent.

    Used by core/tenancy to resolve a role *name* (e.g. `"engineer"`, as
    supplied in an `AccessRuleCreate`/`InvitationCreate` request) into the
    `grants_role_id` actually stored -- a read-only reference-data lookup, not
    a tenant-scoped query, since the `roles` catalog itself is global
    (DATABASE_DESIGN.md).
    """
    stmt = select(Role).where(Role.name == name)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_user_role(
    session: AsyncSession,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    role_id: uuid.UUID,
) -> UserRole | None:
    """Fetch one (user, organization, role) assignment row, or None if it
    doesn't exist -- lets callers make role assignment idempotent by checking
    first, rather than relying on catching an integrity error.
    """
    return await session.get(UserRole, (user_id, organization_id, role_id))


async def insert_user_role(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    role_id: uuid.UUID,
) -> None:
    """Assign `role_id` to `user_id` within `organization_id`.

    Not idempotent by itself -- inserting a duplicate composite key raises an
    integrity error. Callers are expected to check `get_user_role` first
    (the same "check, then act" pattern used elsewhere in this module, e.g.
    `resolve_identity`'s `is_active` check) rather than this function
    silently swallowing a conflict.
    """
    session.add(UserRole(user_id=user_id, organization_id=organization_id, role_id=role_id))
    await session.flush()


async def get_permission_codes(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> set[str]:
    """Return the flattened set of permission codes granted to `user_id`
    *within `organization_id`*.

    Joins `user_roles -> role_permissions -> permissions`, filtered by both
    `user_id` and `organization_id`, and de-duplicates: two roles granting the
    same permission collapse to one code. This set is what the service loads
    into `Identity.permissions` -- scoped to a single organization, never
    merged across every organization a user might belong to.
    """
    stmt = (
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(UserRole, UserRole.role_id == RolePermission.role_id)
        .where(
            UserRole.user_id == user_id,
            UserRole.organization_id == organization_id,
        )
        .distinct()
    )
    result = await session.execute(stmt)
    return set(result.scalars().all())
