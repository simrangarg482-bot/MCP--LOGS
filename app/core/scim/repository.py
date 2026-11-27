"""Persistence for core/scim. Pure data access: one statement per function, ORM rows in and out, no rules.

Every table here is FORCE-RLS: the caller must already have set the organization as the tenant context
(the SCIM request dependency and the admin service both do). The ONE exception is `authenticate_token`,
which goes through the narrow SECURITY DEFINER lookup because a bearer token arrives before any tenant
context exists."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import and_, exists, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.core_models import User, UserRole
from app.database.models.tenancy_models import (
    OrganizationMemberState,
    ScimConfiguration,
    ScimIdentity,
    ScimPendingAction,
    ScimToken,
)

# ---------------------------------------------------------------------------------------------- configuration


async def get_config(session: AsyncSession, organization_id: uuid.UUID) -> ScimConfiguration | None:
    return (
        await session.execute(
            select(ScimConfiguration)
            .where(ScimConfiguration.organization_id == organization_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def get_or_create_config(session: AsyncSession, organization_id: uuid.UUID) -> ScimConfiguration:
    existing = await get_config(session, organization_id)
    if existing is not None:
        return existing
    row = ScimConfiguration(organization_id=organization_id)
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def record_outcome(
    session: AsyncSession, organization_id: uuid.UUID, *, now: datetime, error_code: str | None
) -> None:
    """Health fields on the configuration: every request stamps `last_request_at`; a success stamps
    `last_success_at`; an error stamps `last_error_*`."""
    values: dict = {"last_request_at": now}
    if error_code is None:
        values["last_success_at"] = now
    else:
        values.update(last_error_code=error_code, last_error_at=now)
    await session.execute(
        update(ScimConfiguration).where(ScimConfiguration.organization_id == organization_id).values(**values)
    )


# ---------------------------------------------------------------------------------------------- tokens


async def authenticate_token(session: AsyncSession, token_hash: str):
    """`(token_id, organization_id, expires_at, revoked_at)` for a presented token's hash, or None. Bypasses
    RLS through the narrow SECURITY DEFINER function (no tenant context exists yet)."""
    return (
        await session.execute(
            text("SELECT * FROM public.scim_authenticate_token(:h)"), {"h": token_hash}
        )
    ).first()


async def insert_token(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    name: str,
    token_hash: str,
    token_prefix: str,
    created_by: uuid.UUID | None,
    expires_at: datetime,
) -> ScimToken:
    row = ScimToken(
        organization_id=organization_id, name=name, token_hash=token_hash, token_prefix=token_prefix,
        created_by=created_by, expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def list_tokens(session: AsyncSession, organization_id: uuid.UUID) -> list[ScimToken]:
    return list(
        (
            await session.execute(
                select(ScimToken)
                .where(ScimToken.organization_id == organization_id)
                .order_by(ScimToken.created_at.desc())
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def get_token(session: AsyncSession, organization_id: uuid.UUID, token_id: uuid.UUID) -> ScimToken | None:
    return (
        await session.execute(
            select(ScimToken)
            .where(ScimToken.organization_id == organization_id, ScimToken.id == token_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def count_active_tokens(session: AsyncSession, organization_id: uuid.UUID, *, now: datetime) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(ScimToken)
                .where(
                    ScimToken.organization_id == organization_id,
                    ScimToken.revoked_at.is_(None),
                    ScimToken.expires_at > now,
                )
            )
        ).scalar_one()
    )


async def touch_token(session: AsyncSession, token_id: uuid.UUID, *, now: datetime) -> None:
    await session.execute(update(ScimToken).where(ScimToken.id == token_id).values(last_used_at=now))


# ---------------------------------------------------------------------------------------------- identities


async def get_identity(
    session: AsyncSession, organization_id: uuid.UUID, identity_id: uuid.UUID, *, lock: bool = False
) -> ScimIdentity | None:
    stmt = select(ScimIdentity).where(
        ScimIdentity.organization_id == organization_id,
        ScimIdentity.id == identity_id,
        ScimIdentity.deleted_at.is_(None),
    )
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()


async def find_identity_by_user_name(
    session: AsyncSession, organization_id: uuid.UUID, key: str
) -> ScimIdentity | None:
    return (
        await session.execute(
            select(ScimIdentity)
            .where(
                ScimIdentity.organization_id == organization_id,
                ScimIdentity.user_name_key == key,
                ScimIdentity.deleted_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def find_identity_by_external_id(
    session: AsyncSession, organization_id: uuid.UUID, external_id: str
) -> ScimIdentity | None:
    return (
        await session.execute(
            select(ScimIdentity)
            .where(
                ScimIdentity.organization_id == organization_id,
                ScimIdentity.external_id == external_id,
                ScimIdentity.deleted_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def find_identity_by_user(
    session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID
) -> ScimIdentity | None:
    return (
        await session.execute(
            select(ScimIdentity)
            .where(
                ScimIdentity.organization_id == organization_id,
                ScimIdentity.user_id == user_id,
                ScimIdentity.deleted_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def list_identities(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    offset: int,
    limit: int,
    user_name_key: str | None = None,
    external_id: str | None = None,
    status: str | None = None,
) -> tuple[list[ScimIdentity], int]:
    conditions = [ScimIdentity.organization_id == organization_id, ScimIdentity.deleted_at.is_(None)]
    if user_name_key is not None:
        conditions.append(ScimIdentity.user_name_key == user_name_key)
    if external_id is not None:
        conditions.append(ScimIdentity.external_id == external_id)
    if status == "pending":
        conditions.append(ScimIdentity.user_id.is_(None))
    elif status == "linked":
        conditions.append(ScimIdentity.user_id.is_not(None))
    total = int((await session.execute(select(func.count()).select_from(ScimIdentity).where(*conditions))).scalar_one())
    rows = (
        await session.execute(
            select(ScimIdentity)
            .where(*conditions)
            .order_by(ScimIdentity.created_at, ScimIdentity.id)
            .offset(offset)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
    ).scalars()
    return list(rows), total


async def insert_identity(session: AsyncSession, **values) -> ScimIdentity:
    row = ScimIdentity(**values)
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def find_candidate_members(
    session: AsyncSession, organization_id: uuid.UUID, user_name_key: str
) -> list[tuple[uuid.UUID, str, str]]:
    """Members whose email equals the SCIM userName: a HINT for the administrator who links by hand. It
    links nothing by itself."""
    rows = await session.execute(
        select(User.id, User.email, User.display_name)
        .where(
            func.lower(User.email) == user_name_key,
            exists().where(UserRole.user_id == User.id, UserRole.organization_id == organization_id),
        )
        .limit(5)
    )
    return [(r[0], r[1], r[2]) for r in rows.all()]


# ---------------------------------------------------------------------------------------------- members


async def count_active_members(session: AsyncSession, organization_id: uuid.UUID) -> int:
    """People who hold a role here and are not deactivated."""
    return int(
        (
            await session.execute(
                select(func.count(func.distinct(UserRole.user_id))).where(
                    UserRole.organization_id == organization_id,
                    ~exists().where(
                        OrganizationMemberState.organization_id == UserRole.organization_id,
                        OrganizationMemberState.user_id == UserRole.user_id,
                        OrganizationMemberState.status == "deactivated",
                    ),
                )
            )
        ).scalar_one()
    )


async def count_recent_scim_deactivations(
    session: AsyncSession, organization_id: uuid.UUID, *, since: datetime
) -> int:
    """Members currently deactivated BY SCIM since `since` (the mass-deactivation guard's counter)."""
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(OrganizationMemberState)
                .where(
                    OrganizationMemberState.organization_id == organization_id,
                    OrganizationMemberState.status == "deactivated",
                    OrganizationMemberState.source == "scim",
                    OrganizationMemberState.deactivated_at >= since,
                )
            )
        ).scalar_one()
    )


# ---------------------------------------------------------------------------------------------- pending actions


async def get_pending_for_identity(
    session: AsyncSession, organization_id: uuid.UUID, identity_id: uuid.UUID
) -> ScimPendingAction | None:
    return (
        await session.execute(
            select(ScimPendingAction)
            .where(
                ScimPendingAction.organization_id == organization_id,
                ScimPendingAction.scim_identity_id == identity_id,
                ScimPendingAction.status == "pending",
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def insert_pending(session: AsyncSession, **values) -> ScimPendingAction:
    row = ScimPendingAction(**values)
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_pending(
    session: AsyncSession, organization_id: uuid.UUID, pending_id: uuid.UUID, *, lock: bool = False
) -> ScimPendingAction | None:
    stmt = select(ScimPendingAction).where(
        ScimPendingAction.organization_id == organization_id, ScimPendingAction.id == pending_id
    )
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()


async def list_pending(
    session: AsyncSession, organization_id: uuid.UUID, *, status: str = "pending"
) -> list[tuple[ScimPendingAction, ScimIdentity]]:
    rows = await session.execute(
        select(ScimPendingAction, ScimIdentity)
        .join(ScimIdentity, ScimIdentity.id == ScimPendingAction.scim_identity_id)
        .where(ScimPendingAction.organization_id == organization_id, ScimPendingAction.status == status)
        .order_by(ScimPendingAction.created_at)
    )
    return [(r[0], r[1]) for r in rows.all()]


async def deactivated_user_ids(
    session: AsyncSession, organization_id: uuid.UUID, user_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """Which of `user_ids` are currently deactivated in this organization (one query for a whole page)."""
    if not user_ids:
        return set()
    rows = await session.execute(
        select(OrganizationMemberState.user_id).where(
            OrganizationMemberState.organization_id == organization_id,
            OrganizationMemberState.status == "deactivated",
            OrganizationMemberState.user_id.in_(user_ids),
        )
    )
    return {r[0] for r in rows.all()}


# ---------------------------------------------------------------------------------------------- groups

from sqlalchemy import delete  # noqa: E402

from app.database.models.core_models import Role  # noqa: E402
from app.database.models.tenancy_models import (  # noqa: E402
    ScimGroup,
    ScimGroupMember,
    ScimGroupRoleMapping,
    ScimRoleGrant,
)


async def get_group(
    session: AsyncSession, organization_id: uuid.UUID, group_id: uuid.UUID, *, lock: bool = False
) -> ScimGroup | None:
    stmt = select(ScimGroup).where(ScimGroup.organization_id == organization_id, ScimGroup.id == group_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()


async def find_group_by_name(session: AsyncSession, organization_id: uuid.UUID, key: str) -> ScimGroup | None:
    return (
        await session.execute(
            select(ScimGroup)
            .where(ScimGroup.organization_id == organization_id, ScimGroup.display_name_key == key)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def find_group_by_external_id(
    session: AsyncSession, organization_id: uuid.UUID, external_id: str
) -> ScimGroup | None:
    return (
        await session.execute(
            select(ScimGroup)
            .where(ScimGroup.organization_id == organization_id, ScimGroup.external_id == external_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def list_groups(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    offset: int,
    limit: int,
    name_key: str | None = None,
    external_id: str | None = None,
) -> tuple[list[ScimGroup], int]:
    conditions = [ScimGroup.organization_id == organization_id]
    if name_key is not None:
        conditions.append(ScimGroup.display_name_key == name_key)
    if external_id is not None:
        conditions.append(ScimGroup.external_id == external_id)
    total = int((await session.execute(select(func.count()).select_from(ScimGroup).where(*conditions))).scalar_one())
    rows = (
        await session.execute(
            select(ScimGroup)
            .where(*conditions)
            .order_by(ScimGroup.created_at, ScimGroup.id)
            .offset(offset)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
    ).scalars()
    return list(rows), total


async def insert_group(session: AsyncSession, **values) -> ScimGroup:
    row = ScimGroup(**values)
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def delete_group(session: AsyncSession, group: ScimGroup) -> None:
    await session.delete(group)
    await session.flush()


async def group_member_identity_ids(
    session: AsyncSession, group_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    out: dict[uuid.UUID, list[uuid.UUID]] = {g: [] for g in group_ids}
    if not group_ids:
        return out
    rows = await session.execute(
        select(ScimGroupMember.group_id, ScimGroupMember.scim_identity_id)
        .where(ScimGroupMember.group_id.in_(group_ids))
        .order_by(ScimGroupMember.added_at, ScimGroupMember.scim_identity_id)
    )
    for group_id, identity_id in rows.all():
        out[group_id].append(identity_id)
    return out


async def add_group_member(
    session: AsyncSession, organization_id: uuid.UUID, group_id: uuid.UUID, identity_id: uuid.UUID
) -> bool:
    if await session.get(ScimGroupMember, (group_id, identity_id)) is not None:
        return False
    session.add(ScimGroupMember(group_id=group_id, scim_identity_id=identity_id, organization_id=organization_id))
    await session.flush()
    return True


async def remove_group_member(session: AsyncSession, group_id: uuid.UUID, identity_id: uuid.UUID) -> bool:
    row = await session.get(ScimGroupMember, (group_id, identity_id))
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def remove_identity_from_all_groups(session: AsyncSession, identity_id: uuid.UUID) -> None:
    await session.execute(delete(ScimGroupMember).where(ScimGroupMember.scim_identity_id == identity_id))
    await session.flush()


async def get_role_mappings(
    session: AsyncSession, organization_id: uuid.UUID, group_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    out: dict[uuid.UUID, list[uuid.UUID]] = {g: [] for g in group_ids}
    if not group_ids:
        return out
    rows = await session.execute(
        select(ScimGroupRoleMapping.group_id, ScimGroupRoleMapping.role_id).where(
            ScimGroupRoleMapping.organization_id == organization_id, ScimGroupRoleMapping.group_id.in_(group_ids)
        )
    )
    for group_id, role_id in rows.all():
        out[group_id].append(role_id)
    return out


async def set_role_mappings(
    session: AsyncSession,
    organization_id: uuid.UUID,
    group_id: uuid.UUID,
    role_ids: set[uuid.UUID],
    created_by: uuid.UUID | None,
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Make the group's mapped roles exactly `role_ids`. Returns (added, removed)."""
    current = set((await get_role_mappings(session, organization_id, [group_id]))[group_id])
    added, removed = role_ids - current, current - role_ids
    if removed:
        await session.execute(
            delete(ScimGroupRoleMapping).where(
                ScimGroupRoleMapping.group_id == group_id, ScimGroupRoleMapping.role_id.in_(removed)
            )
        )
    for role_id in added:
        session.add(
            ScimGroupRoleMapping(
                group_id=group_id, role_id=role_id, organization_id=organization_id, created_by=created_by
            )
        )
    await session.flush()
    return added, removed


async def desired_grants_for_identity(
    session: AsyncSession, organization_id: uuid.UUID, identity_id: uuid.UUID
) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """(group, role) pairs the identity's group memberships entitle it to under the current mappings."""
    rows = await session.execute(
        select(ScimGroupMember.group_id, ScimGroupRoleMapping.role_id)
        .join(ScimGroupRoleMapping, ScimGroupRoleMapping.group_id == ScimGroupMember.group_id)
        .where(ScimGroupMember.scim_identity_id == identity_id, ScimGroupMember.organization_id == organization_id)
    )
    return {(r[0], r[1]) for r in rows.all()}


async def get_grants_for_user(
    session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID
) -> set[tuple[uuid.UUID, uuid.UUID]]:
    rows = await session.execute(
        select(ScimRoleGrant.group_id, ScimRoleGrant.role_id).where(
            ScimRoleGrant.organization_id == organization_id, ScimRoleGrant.user_id == user_id
        )
    )
    return {(r[0], r[1]) for r in rows.all()}


async def insert_grant(
    session: AsyncSession, organization_id: uuid.UUID, group_id: uuid.UUID, user_id: uuid.UUID, role_id: uuid.UUID
) -> None:
    session.add(ScimRoleGrant(group_id=group_id, user_id=user_id, role_id=role_id, organization_id=organization_id))
    await session.flush()


async def delete_grant(session: AsyncSession, group_id: uuid.UUID, user_id: uuid.UUID, role_id: uuid.UUID) -> None:
    await session.execute(
        delete(ScimRoleGrant).where(
            ScimRoleGrant.group_id == group_id, ScimRoleGrant.user_id == user_id, ScimRoleGrant.role_id == role_id
        )
    )
    await session.flush()


async def grant_user_ids_for_group(session: AsyncSession, group_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await session.execute(select(ScimRoleGrant.user_id).where(ScimRoleGrant.group_id == group_id))
    return set(rows.scalars().all())


async def linked_user_ids_for_group(session: AsyncSession, group_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await session.execute(
        select(ScimIdentity.user_id)
        .join(ScimGroupMember, ScimGroupMember.scim_identity_id == ScimIdentity.id)
        .where(
            ScimGroupMember.group_id == group_id,
            ScimIdentity.user_id.is_not(None),
            ScimIdentity.deleted_at.is_(None),
        )
    )
    return set(rows.scalars().all())


async def user_ids_for_identities(session: AsyncSession, identity_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    if not identity_ids:
        return set()
    rows = await session.execute(
        select(ScimIdentity.user_id).where(ScimIdentity.id.in_(identity_ids), ScimIdentity.user_id.is_not(None))
    )
    return set(rows.scalars().all())


async def user_role_provenance(
    session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID
) -> dict[uuid.UUID, str]:
    rows = await session.execute(
        select(UserRole.role_id, UserRole.granted_via).where(
            UserRole.organization_id == organization_id, UserRole.user_id == user_id
        )
    )
    return {r[0]: r[1] for r in rows.all()}


async def remove_user_role(
    session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID, role_id: uuid.UUID
) -> None:
    await session.execute(
        delete(UserRole).where(
            UserRole.organization_id == organization_id, UserRole.user_id == user_id, UserRole.role_id == role_id
        )
    )
    await session.flush()


async def visible_roles(
    session: AsyncSession, organization_id: uuid.UUID, role_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not role_ids:
        return {}
    rows = await session.execute(
        select(Role.id, Role.name).where(
            Role.id.in_(role_ids), or_(Role.organization_id.is_(None), Role.organization_id == organization_id)
        )
    )
    return {r[0]: r[1] for r in rows.all()}
