"""Persistence for core/users -- users, roles, permissions.

Owned by: core/users. Pure data access: each function issues one query and
returns ORM rows or plain scalar values; identity assembly and authorization
decisions live in service.py.

The two resolution queries (`get_role_names`, `get_permission_codes`) are the
performance-relevant heart of RBAC: they let the service resolve an identity
in two small indexed queries once per request, so every downstream
`authorize()` is then a pure in-memory set check with no further DB hits.
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


async def get_role_names(session: AsyncSession, user_id: uuid.UUID) -> Sequence[str]:
    """Return the names of the roles assigned to `user_id`.

    Joins `user_roles -> roles`. Empty sequence if the user has no roles.
    """
    stmt = (
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
        .order_by(Role.name)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_permission_codes(session: AsyncSession, user_id: uuid.UUID) -> set[str]:
    """Return the flattened set of permission codes granted to `user_id`.

    Joins `user_roles -> role_permissions -> permissions` and de-duplicates:
    two roles granting the same permission collapse to one code. This set is
    what the service loads into `Identity.permissions`.
    """
    stmt = (
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(UserRole, UserRole.role_id == RolePermission.role_id)
        .where(UserRole.user_id == user_id)
        .distinct()
    )
    result = await session.execute(stmt)
    return set(result.scalars().all())