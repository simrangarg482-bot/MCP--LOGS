"""SCIM Groups and role synchronization.

EXACT SEMANTICS (the same text is in docs/operations/scim-provisioning.md)

1. A group pushed by the identity provider GRANTS NOTHING by itself. An administrator maps it, explicitly and
   per organization, to one or more roles (`scim_group_role_mappings`, keyed by the group's id so a rename in
   the identity provider changes nothing). SCIM can never choose a role: only a mapping can, and a mapping can
   only be created by an administrator who holds every permission of the role (no privilege escalation).
2. Group membership comes from the identity provider and is stored against SCIM identities, linked or not.
   Roles are granted only to a LINKED member of this organization (an identity that is still pending is
   remembered, and gets its roles the moment it is linked).
3. PROVENANCE. `user_roles.granted_via` is `manual` or `scim`. Synchronization creates `scim` rows only, and
   removes `scim` rows only. A role the member already held manually stays manual (an administrator's grant
   always wins and is never taken away); a manual grant of a SCIM role converts it to manual.
4. Several groups may grant the same role. Each (group, member, role) is a row in `scim_role_grants`; the role
   stays for as long as ANY group still grants it.
5. Removing a member from a group (or deleting the group, or removing its mapping) withdraws exactly the roles
   that group granted and nothing else -- never a manual role, never a role another group still grants.
6. The member is NOT deactivated just because one group changed. Only when the withdrawal would leave them
   with NO role at all are they deactivated for this organization (source `scim`, reason recorded, subject to
   the same mass-deactivation guard and last-administrator guard as every other SCIM deactivation). Their roles
   are left in place in that case, so an administrator can reactivate them without re-granting anything.
7. A withdrawal that would remove the organization's last administrator is REFUSED and audited: the role stays.
   It is retried by the next synchronization of that member.
8. Administrators cannot hand-remove a role a group currently grants (it would be put straight back); they
   change the group or the mapping instead (`member.role_group_managed`).
9. SCIM never creates a user account.

Every function here is idempotent: replaying the same request, or the same group event arriving twice or out of
order, converges on the same state, because `reconcile_user` recomputes a member's entitlements from the
current memberships and mappings rather than applying deltas.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.service import record_audit_event
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.scim import repository, schemas
from app.core.scim.errors import ScimError
from app.core.users import role_repository, role_service
from app.core.users import repository as users_repository
from app.core.users import service as users_service
from app.core.users import membership
from app.core.users.permissions import ADMIN_PERMISSION
from app.database.models.tenancy_models import ScimConfiguration, ScimGroup
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity

logger = get_logger(__name__)

MAX_ROLES_PER_GROUP = 20


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _KeepsAdmin(Exception):
    """Raised inside a savepoint to undo a role removal that would leave the organization without an administrator."""


# ---------------------------------------------------------------------------------------------- guard


async def guard_trips(session: AsyncSession, config: ScimConfiguration | None, organization_id: uuid.UUID) -> bool:
    """The mass-deactivation guard: more SCIM deactivations inside the window than
    max(minimum, percent of active members) are HELD for an administrator."""
    minimum = config.guard_min_absolute if config else 5
    percent = config.guard_percent if config else 20
    window = config.guard_window_minutes if config else 60
    active_members = await repository.count_active_members(session, organization_id)
    limit = max(minimum, math.ceil(active_members * percent / 100))
    recent = await repository.count_recent_scim_deactivations(
        session, organization_id, since=_now() - timedelta(minutes=window)
    )
    return recent + 1 > limit


# ---------------------------------------------------------------------------------------------- synchronization


async def reconcile_user(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    config: ScimConfiguration | None = None,
) -> None:
    """Bring ONE member's SCIM-granted roles in line with their current group memberships and the current
    mappings. Recomputes from state (never from deltas), so it is idempotent and order-independent. A
    deactivated member is left alone (their roles are re-evaluated when they are reactivated)."""
    # Two requests changing the same person (two groups, or a group and a link) must take turns: each then
    # recomputes from the other's committed result instead of both inserting the same grant row.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"scim-reconcile:{organization_id}:{user_id}"},
    )
    if config is None:
        config = await repository.get_config(session, organization_id)
    state = await users_repository.get_member_state(session, organization_id, user_id)
    if state is not None and state.status == "deactivated":
        return
    is_member = await users_repository.is_organization_member(session, user_id, organization_id)
    identity = await repository.find_identity_by_user(session, organization_id, user_id) if is_member else None
    desired = (
        await repository.desired_grants_for_identity(session, organization_id, identity.id) if identity else set()
    )

    current = await repository.get_grants_for_user(session, organization_id, user_id)
    for group_id, role_id in sorted(desired - current, key=str):
        await repository.insert_grant(session, organization_id, group_id, user_id, role_id)
    for group_id, role_id in sorted(current - desired, key=str):
        await repository.delete_grant(session, group_id, user_id, role_id)
    if not is_member:
        return

    desired_roles = {role_id for _group, role_id in desired}
    held = await repository.user_role_provenance(session, organization_id, user_id)
    for role_id in sorted(desired_roles - set(held), key=str):
        await users_service.assign_role(
            session, user_id=user_id, organization_id=organization_id, role_id=role_id, granted_via="scim"
        )
        await record_audit_event(
            session, actor, action="scim.role_granted", resource_type="user", resource_id=user_id,
            metadata={"role_id": str(role_id), "via": "group"},
        )
        held[role_id] = "scim"

    stale = sorted((r for r, via in held.items() if via == "scim" and r not in desired_roles), key=str)
    if stale:
        await _withdraw(session, actor, organization_id, user_id, config, held=set(held), stale=stale)


async def _withdraw(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    config: ScimConfiguration | None,
    *,
    held: set[uuid.UUID],
    stale: list[uuid.UUID],
) -> None:
    """Remove SCIM-granted roles no group grants any more -- never one the member holds manually."""
    await users_repository.acquire_org_admin_lock(session, organization_id)
    if not (held - set(stale)):
        # Nothing would be left: the person has lost every route to access. Deactivate (organization-specific),
        # behind the same guards as any SCIM deactivation. The roles stay, for a later reactivation.
        if await guard_trips(session, config, organization_id):
            await record_audit_event(
                session, actor, action="scim.role_revoke_held", resource_type="user", resource_id=user_id,
                metadata={"reason": "mass_deactivation_guard", "role_ids": [str(r) for r in stale]},
            )
            return
        try:
            await membership.deactivate_member_as_system(
                session, actor, organization_id, user_id,
                reason="Removed from every identity-provider group that granted access", source="scim",
                extra_metadata={"via": "group_sync"},
            )
        except ConflictError as exc:
            if exc.error_code == "member.already_deactivated":
                return
            if exc.error_code != "org.last_admin":
                raise
            await record_audit_event(
                session, actor, action="scim.role_revoke_refused", resource_type="user", resource_id=user_id,
                metadata={"reason": "last_admin", "role_ids": [str(r) for r in stale]},
            )
        return

    for role_id in stale:
        before = await users_repository.list_org_admin_user_ids(session, organization_id, lock=True)
        try:
            async with session.begin_nested():
                await repository.remove_user_role(session, organization_id, user_id, role_id)
                after = await users_repository.list_org_admin_user_ids(session, organization_id, lock=False)
                if before and not after:
                    raise _KeepsAdmin
        except _KeepsAdmin:
            await record_audit_event(
                session, actor, action="scim.role_revoke_refused", resource_type="user", resource_id=user_id,
                metadata={"reason": "last_admin", "role_ids": [str(role_id)]},
            )
            continue
        await record_audit_event(
            session, actor, action="scim.role_revoked", resource_type="user", resource_id=user_id,
            metadata={"role_id": str(role_id), "via": "group"},
        )


async def _reconcile_many(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, user_ids: set[uuid.UUID]
) -> None:
    config = await repository.get_config(session, organization_id)
    for user_id in sorted(user_ids, key=str):
        await reconcile_user(session, actor, organization_id, user_id, config)


# ---------------------------------------------------------------------------------------------- SCIM protocol


async def _resource(session: AsyncSession, base_url: str, group: ScimGroup) -> dict:
    members = (await repository.group_member_identity_ids(session, [group.id]))[group.id]
    return schemas.group_resource(group, members, base_url)


async def _require_group(
    session: AsyncSession, organization_id: uuid.UUID, group_id: str, *, lock: bool = False
) -> ScimGroup:
    try:
        key = uuid.UUID(group_id)
    except ValueError:
        raise ScimError(404, "Group not found.", code="scim.not_found") from None
    group = await repository.get_group(session, organization_id, key, lock=lock)
    if group is None:
        raise ScimError(404, "Group not found.", code="scim.not_found")
    return group


async def get_group(session: AsyncSession, organization_id: uuid.UUID, base_url: str, group_id: str) -> dict:
    return await _resource(session, base_url, await _require_group(session, organization_id, group_id))


async def list_groups(
    session: AsyncSession,
    organization_id: uuid.UUID,
    base_url: str,
    *,
    filter_text: str | None,
    start_index: int | None,
    count: int | None,
) -> dict:
    parsed = schemas.parse_group_filter(filter_text)
    start, size = schemas.clamp_paging(start_index, count)
    rows, total = await repository.list_groups(
        session, organization_id, offset=start - 1, limit=size,
        name_key=schemas.user_name_key(parsed[1]) if parsed and parsed[0] == "displayName" else None,
        external_id=parsed[1] if parsed and parsed[0] == "externalId" else None,
    )
    members = await repository.group_member_identity_ids(session, [g.id for g in rows])
    return schemas.list_response(
        [schemas.group_resource(g, members[g.id], base_url) for g in rows] if size else [], total, start
    )


async def _identity_ids(session: AsyncSession, organization_id: uuid.UUID, values: list[str], *, strict: bool) -> list[uuid.UUID]:
    """Member `value`s -> this organization's SCIM identity ids. An unknown one is an error when adding (the
    provider must create the user first), and silently skipped when removing (removal is idempotent)."""
    out: list[uuid.UUID] = []
    for raw in values:
        try:
            key = uuid.UUID(raw)
        except ValueError:
            key = None
        identity = await repository.get_identity(session, organization_id, key) if key else None
        if identity is None:
            if strict:
                raise ScimError(400, f"Unknown member '{raw[:64]}'.", "invalidValue", code="scim.unknown_member")
            continue
        out.append(identity.id)
    return out


async def _apply_member_ops(
    session: AsyncSession, organization_id: uuid.UUID, group: ScimGroup, ops: list[tuple[str, list[str] | None]]
) -> set[uuid.UUID]:
    """Apply the membership operations in order; returns the identities whose membership changed."""
    changed: set[uuid.UUID] = set()
    members = set((await repository.group_member_identity_ids(session, [group.id]))[group.id])
    for op, values in ops:
        if op == "replace":
            target = set(await _identity_ids(session, organization_id, values or [], strict=True))
            for identity_id in sorted(members - target, key=str):
                await repository.remove_group_member(session, group.id, identity_id)
                changed.add(identity_id)
            for identity_id in sorted(target - members, key=str):
                await repository.add_group_member(session, organization_id, group.id, identity_id)
                changed.add(identity_id)
            members = target
        elif op == "add":
            for identity_id in await _identity_ids(session, organization_id, values or [], strict=True):
                if await repository.add_group_member(session, organization_id, group.id, identity_id):
                    changed.add(identity_id)
                    members.add(identity_id)
        else:  # remove
            targets = members if values is None else set(await _identity_ids(session, organization_id, values, strict=False))
            for identity_id in sorted(targets & members, key=str):
                await repository.remove_group_member(session, group.id, identity_id)
                changed.add(identity_id)
                members.discard(identity_id)
    return changed


async def _after_membership_change(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, group: ScimGroup, changed: set[uuid.UUID]
) -> None:
    if not changed:
        return
    await record_audit_event(
        session, actor, action="scim.group_members_changed", resource_type="scim_group", resource_id=group.id,
        metadata={"group": group.display_name, "changed": len(changed)},
    )
    user_ids = await repository.user_ids_for_identities(session, list(changed))
    await _reconcile_many(session, actor, organization_id, user_ids)


async def create_group(
    session: AsyncSession, organization_id: uuid.UUID, actor: Identity, base_url: str, payload: object
) -> dict:
    fields = schemas.parse_group_payload(payload)
    assert fields.display_name is not None
    key = schemas.user_name_key(fields.display_name)
    if await repository.find_group_by_name(session, organization_id, key) is not None:
        raise ScimError(409, "A group with that displayName already exists.", "uniqueness", code="scim.uniqueness")
    if fields.external_id and await repository.find_group_by_external_id(session, organization_id, fields.external_id):
        raise ScimError(409, "A group with that externalId already exists.", "uniqueness", code="scim.uniqueness")
    async with session.begin_nested():
        group = await repository.insert_group(
            session, organization_id=organization_id, display_name=fields.display_name,
            display_name_key=key, external_id=fields.external_id,
        )
        await record_audit_event(
            session, actor, action="scim.group_created", resource_type="scim_group", resource_id=group.id,
            metadata={"group": group.display_name},
        )
        changed = await _apply_member_ops(session, organization_id, group, fields.member_ops)
        await _after_membership_change(session, actor, organization_id, group, changed)
    return await _resource(session, base_url, group)


async def _update_group(
    session: AsyncSession, organization_id: uuid.UUID, actor: Identity, base_url: str, group: ScimGroup,
    fields: schemas.GroupFields,
) -> dict:
    async with session.begin_nested():
        if "displayName" in fields.provided and fields.display_name:
            key = schemas.user_name_key(fields.display_name)
            if key != group.display_name_key:
                clash = await repository.find_group_by_name(session, organization_id, key)
                if clash is not None and clash.id != group.id:
                    raise ScimError(409, "A group with that displayName already exists.", "uniqueness", code="scim.uniqueness")
            if fields.display_name != group.display_name:
                await record_audit_event(
                    session, actor, action="scim.group_renamed", resource_type="scim_group", resource_id=group.id,
                    metadata={"from": group.display_name, "to": fields.display_name},
                )
            group.display_name, group.display_name_key = fields.display_name, key
        if "externalId" in fields.provided and fields.external_id != group.external_id:
            if fields.external_id is not None:
                clash = await repository.find_group_by_external_id(session, organization_id, fields.external_id)
                if clash is not None and clash.id != group.id:
                    raise ScimError(409, "A group with that externalId already exists.", "uniqueness", code="scim.uniqueness")
            group.external_id = fields.external_id
        group.updated_at = _now()
        await session.flush()
        changed = await _apply_member_ops(session, organization_id, group, fields.member_ops)
        await _after_membership_change(session, actor, organization_id, group, changed)
    return await _resource(session, base_url, group)


async def replace_group(
    session: AsyncSession, organization_id: uuid.UUID, actor: Identity, base_url: str, group_id: str, payload: object
) -> dict:
    group = await _require_group(session, organization_id, group_id, lock=True)
    return await _update_group(session, organization_id, actor, base_url, group, schemas.parse_group_payload(payload))


async def patch_group(
    session: AsyncSession, organization_id: uuid.UUID, actor: Identity, base_url: str, group_id: str, payload: object
) -> dict:
    group = await _require_group(session, organization_id, group_id, lock=True)
    return await _update_group(session, organization_id, actor, base_url, group, schemas.parse_group_patch(payload))


async def delete_group(session: AsyncSession, organization_id: uuid.UUID, actor: Identity, group_id: str) -> None:
    """Deleting a group withdraws exactly the roles it granted (rule 5); it deletes no member and no data."""
    group = await _require_group(session, organization_id, group_id, lock=True)
    async with session.begin_nested():
        affected = await repository.grant_user_ids_for_group(session, group.id) | await repository.linked_user_ids_for_group(
            session, group.id
        )
        name, gid = group.display_name, group.id
        await repository.delete_group(session, group)
        await record_audit_event(
            session, actor, action="scim.group_deleted", resource_type="scim_group", resource_id=gid,
            metadata={"group": name, "members_affected": len(affected)},
        )
        await _reconcile_many(session, actor, organization_id, affected)


async def forget_identity_memberships(
    session: AsyncSession, organization_id: uuid.UUID, actor: Identity, identity_id: uuid.UUID, user_id: uuid.UUID | None
) -> None:
    """The identity provider deleted the user: drop their group memberships (their access was just deactivated)."""
    await repository.remove_identity_from_all_groups(session, identity_id)
    if user_id is not None:
        await reconcile_user(session, actor, organization_id, user_id)


# ---------------------------------------------------------------------------------------------- administration


class GroupRole(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    name: str


class ScimGroupInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    display_name: str
    external_id: str | None
    member_count: int
    roles: list[GroupRole]
    updated_at: datetime


class ScimGroupRolesUpdate(BaseModel):
    role_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_ROLES_PER_GROUP)


def _manage(actor: Identity, organization_id: uuid.UUID) -> None:
    users_service._ensure_same_organization(actor, organization_id)  # noqa: SLF001 - the shared tenant guard
    users_service.require_permission(actor, ADMIN_PERMISSION)


async def _info(session: AsyncSession, organization_id: uuid.UUID, groups: list[ScimGroup]) -> list[ScimGroupInfo]:
    ids = [g.id for g in groups]
    members = await repository.group_member_identity_ids(session, ids)
    mappings = await repository.get_role_mappings(session, organization_id, ids)
    names = await repository.visible_roles(session, organization_id, {r for rs in mappings.values() for r in rs})
    return [
        ScimGroupInfo(
            id=g.id, display_name=g.display_name, external_id=g.external_id, member_count=len(members[g.id]),
            roles=sorted(
                (GroupRole(id=r, name=names[r]) for r in mappings[g.id] if r in names), key=lambda r: r.name.lower()
            ),
            updated_at=g.updated_at,
        )
        for g in groups
    ]


async def list_groups_admin(session: AsyncSession, actor: Identity, organization_id: uuid.UUID) -> list[ScimGroupInfo]:
    _manage(actor, organization_id)
    rows, _total = await repository.list_groups(session, organization_id, offset=0, limit=500)
    return await _info(session, organization_id, rows)


async def set_group_roles(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, group_id: uuid.UUID, data: ScimGroupRolesUpdate
) -> ScimGroupInfo:
    """Make `data.role_ids` exactly the roles this group grants, then reconcile everyone it touches."""
    _manage(actor, organization_id)
    group = await repository.get_group(session, organization_id, group_id, lock=True)
    if group is None:
        raise NotFoundError("Group not found.", error_code="scim.group_not_found")
    wanted = set(data.role_ids)
    visible = await repository.visible_roles(session, organization_id, wanted)
    if set(visible) != wanted:
        raise ValidationError("Unknown role.", error_code="role.not_found")
    current = set((await repository.get_role_mappings(session, organization_id, [group.id]))[group.id])
    for role_id in wanted - current:  # no privilege escalation: you can only map a role you could grant yourself
        await role_service.assert_actor_can_grant_role(session, actor, organization_id, role_id)
    added, removed = await repository.set_role_mappings(session, organization_id, group.id, wanted, actor.user_id)
    if added or removed:
        names = await repository.visible_roles(session, organization_id, added | removed)
        await record_audit_event(
            session, actor, action="scim.group_roles_updated", resource_type="scim_group", resource_id=group.id,
            metadata={
                "group": group.display_name,
                "added": sorted(names.get(r, str(r)) for r in added),
                "removed": sorted(names.get(r, str(r)) for r in removed),
            },
        )
        affected = await repository.linked_user_ids_for_group(session, group.id) | await repository.grant_user_ids_for_group(
            session, group.id
        )
        await _reconcile_many(session, actor, organization_id, affected)
    return (await _info(session, organization_id, [group]))[0]
