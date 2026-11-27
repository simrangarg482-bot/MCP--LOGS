"""Queries for organization-scoped role management.

Owned by `core.users` (it owns `roles`, `permissions`, `role_permissions`,
`user_roles`). Callers outside this package go through `role_service`.

Every function takes the organization explicitly and filters on it in SQL even
though row-level security enforces the same rule underneath: the application
filter is the primary check, RLS the backstop (the same convention as the rest
of this package). "Visible to an organization" always means *built-in roles
(`organization_id IS NULL`) plus that organization's own roles*.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.core_models import Permission, Role, RolePermission, User, UserRole
from app.database.models.tenancy_models import (
    Invitation,
    OrganizationAccessRule,
    Project,
    ProjectMembership,
    ScimGroupRoleMapping,
    ScimRoleGrant,
)


def _visible(organization_id: uuid.UUID):
    return or_(Role.organization_id.is_(None), Role.organization_id == organization_id)


async def list_visible_roles(session: AsyncSession, organization_id: uuid.UUID) -> Sequence[Role]:
    stmt = (
        select(Role)
        .where(_visible(organization_id))
        # built-ins first, then alphabetical
        .order_by(Role.organization_id.is_not(None), func.lower(Role.name))
    )
    return (await session.execute(stmt)).scalars().all()


async def get_visible_role(
    session: AsyncSession, organization_id: uuid.UUID, role_id: uuid.UUID, *, lock: bool = False
) -> Role | None:
    stmt = select(Role).where(Role.id == role_id, _visible(organization_id))
    if lock:
        stmt = stmt.with_for_update(of=Role)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_visible_roles_by_ids(
    session: AsyncSession, organization_id: uuid.UUID, role_ids: Iterable[uuid.UUID]
) -> Sequence[Role]:
    ids = list(role_ids)
    if not ids:
        return []
    stmt = select(Role).where(Role.id.in_(ids), _visible(organization_id))
    return (await session.execute(stmt)).scalars().all()


async def find_role_by_name(
    session: AsyncSession, organization_id: uuid.UUID, name: str
) -> Role | None:
    """Case-insensitive lookup among roles visible to `organization_id`; an
    organization's own role wins over a built-in of the same name (creation
    forbids that collision, but a built-in added later could still create it).
    """
    stmt = (
        select(Role)
        .where(func.lower(Role.name) == name.strip().lower(), _visible(organization_id))
        .order_by(Role.organization_id.is_(None))  # own role first
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def permission_codes_by_role(
    session: AsyncSession, role_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, ...]]:
    ids = list(role_ids)
    if not ids:
        return {}
    stmt = (
        select(RolePermission.role_id, Permission.code)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(RolePermission.role_id.in_(ids))
    )
    grouped: dict[uuid.UUID, list[str]] = {}
    for role_id, code in (await session.execute(stmt)).all():
        grouped.setdefault(role_id, []).append(code)
    return {role_id: tuple(sorted(codes)) for role_id, codes in grouped.items()}


async def member_counts_by_role(
    session: AsyncSession, organization_id: uuid.UUID
) -> dict[uuid.UUID, int]:
    stmt = (
        select(UserRole.role_id, func.count(func.distinct(UserRole.user_id)))
        .where(UserRole.organization_id == organization_id)
        .group_by(UserRole.role_id)
    )
    return {role_id: int(count) for role_id, count in (await session.execute(stmt)).all()}


async def insert_role(
    session: AsyncSession, *, organization_id: uuid.UUID, name: str, description: str | None
) -> Role:
    role = Role(organization_id=organization_id, name=name, description=description)
    session.add(role)
    await session.flush()
    return role


async def delete_role(session: AsyncSession, role: Role) -> None:
    await session.delete(role)
    await session.flush()


async def set_role_permission_codes(
    session: AsyncSession, role_id: uuid.UUID, codes: Iterable[str]
) -> None:
    """Make `role_id`'s grants exactly `codes` (add the missing, drop the rest).

    The caller has already validated `codes` against the assignable catalog;
    missing `permissions` rows are created here (the catalog is seeded by a
    migration, so this only matters on databases built without it).
    """
    wanted = set(codes)
    existing_rows = (
        await session.execute(
            select(RolePermission.permission_id, Permission.code)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(RolePermission.role_id == role_id)
        )
    ).all()
    have = {code: pid for pid, code in existing_rows}

    to_remove = [pid for code, pid in have.items() if code not in wanted]
    if to_remove:
        await session.execute(
            delete(RolePermission).where(
                RolePermission.role_id == role_id, RolePermission.permission_id.in_(to_remove)
            )
        )

    to_add = wanted - have.keys()
    if to_add:
        perm_rows = (
            await session.execute(select(Permission).where(Permission.code.in_(to_add)))
        ).scalars().all()
        found = {p.code: p for p in perm_rows}
        for code in sorted(to_add):
            perm = found.get(code)
            if perm is None:
                perm = Permission(code=code)
                session.add(perm)
                await session.flush()
            session.add(RolePermission(role_id=role_id, permission_id=perm.id))
    await session.flush()


async def role_usage(session: AsyncSession, organization_id: uuid.UUID, role_id: uuid.UUID) -> dict[str, int]:
    """How many things still reference `role_id` (any of them blocks deletion)."""
    members = (
        await session.execute(
            select(func.count(func.distinct(UserRole.user_id))).where(
                UserRole.role_id == role_id, UserRole.organization_id == organization_id
            )
        )
    ).scalar_one()
    project_members = (
        await session.execute(
            select(func.count())
            .select_from(ProjectMembership)
            .join(Project, Project.id == ProjectMembership.project_id)
            .where(ProjectMembership.role_id == role_id, Project.organization_id == organization_id)
        )
    ).scalar_one()
    invitations = (
        await session.execute(
            select(func.count()).select_from(Invitation).where(
                Invitation.grants_role_id == role_id, Invitation.organization_id == organization_id
            )
        )
    ).scalar_one()
    rules = (
        await session.execute(
            select(func.count()).select_from(OrganizationAccessRule).where(
                OrganizationAccessRule.grants_role_id == role_id,
                OrganizationAccessRule.organization_id == organization_id,
            )
        )
    ).scalar_one()
    mappings = (
        await session.execute(
            select(func.count()).select_from(ScimGroupRoleMapping).where(
                ScimGroupRoleMapping.role_id == role_id, ScimGroupRoleMapping.organization_id == organization_id
            )
        )
    ).scalar_one()
    return {
        "members": int(members),
        "project_memberships": int(project_members),
        "invitations": int(invitations),
        "access_rules": int(rules),
        "scim_group_mappings": int(mappings),
    }


async def group_managed_role_ids(
    session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID
) -> set[uuid.UUID]:
    """Roles the member holds BECAUSE a SCIM group grants them right now. An administrator cannot remove
    these by hand: the next synchronization would put them straight back. Change the group instead."""
    rows = await session.execute(
        select(ScimRoleGrant.role_id).where(
            ScimRoleGrant.organization_id == organization_id, ScimRoleGrant.user_id == user_id
        )
    )
    return set(rows.scalars().all())


# --- member role assignments --------------------------------------------------


async def get_member_role_ids(
    session: AsyncSession, organization_id: uuid.UUID
) -> dict[uuid.UUID, tuple[uuid.UUID, ...]]:
    stmt = select(UserRole.user_id, UserRole.role_id).where(
        UserRole.organization_id == organization_id
    )
    grouped: dict[uuid.UUID, list[uuid.UUID]] = {}
    for user_id, role_id in (await session.execute(stmt)).all():
        grouped.setdefault(user_id, []).append(role_id)
    return {user_id: tuple(sorted(ids, key=str)) for user_id, ids in grouped.items()}


async def get_user_role_ids(
    session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID, *, lock: bool = False
) -> set[uuid.UUID]:
    stmt = select(UserRole.role_id).where(
        UserRole.organization_id == organization_id, UserRole.user_id == user_id
    )
    if lock:
        stmt = stmt.with_for_update(of=UserRole)
    return set((await session.execute(stmt)).scalars().all())


async def delete_user_role(
    session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID, role_id: uuid.UUID
) -> None:
    await session.execute(
        delete(UserRole).where(
            UserRole.organization_id == organization_id,
            UserRole.user_id == user_id,
            UserRole.role_id == role_id,
        )
    )
    await session.flush()


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)
