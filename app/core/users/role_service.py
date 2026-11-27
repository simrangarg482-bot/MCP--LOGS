"""Role management: organization-scoped custom roles and member role assignment.

Owned by: core/users. Backs the `/organizations/{id}/roles`, `/permissions` and
`/members/{user_id}/roles` endpoints.

Model (see migration `e8c3a1f5b7d2`):

* **Built-in roles** (`Role.organization_id IS NULL`, today only `admin`) are
  shared by every organization and are read-only to everyone, enforced here
  *and* by row-level security (the application role cannot write them).
* **Custom roles** belong to one organization; its administrators create, edit,
  rename and delete them. Every organization starts with three editable
  starter roles (`viewer`, `engineer`, `reviewer`).
* A user may hold several roles in an organization; their permissions are the
  union. Permissions are resolved from the database on every request
  (`core.users.service.resolve_identity`), so a change takes effect on the
  affected user's very next request.

Authorization rules enforced here (the frontend's checks are only UX):

* every operation requires `tenancy:manage` in the caller's own organization
  (the permission *catalog* is readable by any member);
* no privilege escalation: an administrator can only put a permission on a
  role, or hand someone a role, if they hold every permission involved;
* only catalog permissions are assignable -- internal codes such as
  `system:graph_discovery` are rejected;
* the organization always keeps at least one administrator (someone holding
  `tenancy:manage`): any change that would leave none is refused with
  `org.last_admin`, serialized by the same advisory lock the data-deletion path
  uses; a member can never be left with no role at all;
* a role that is still assigned, invited or referenced by an access rule cannot
  be deleted.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.service import record_audit_event
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.users import repository, role_repository
from app.core.users import service as users_service
from app.core.users.permissions import (
    ADMIN_PERMISSION,
    ASSIGNABLE_PERMISSION_CODES,
    ASSIGNABLE_PERMISSIONS,
    DEFAULT_ROLE_TEMPLATES,
)
from app.core.users.role_schemas import (
    MemberRoles,
    PermissionInfo,
    Role,
    RoleCreate,
    RoleUpdate,
)
from app.database.models.core_models import Role as RoleRow
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity

logger = get_logger(__name__)


# --- guards ------------------------------------------------------------------


def _ensure_same_organization(actor: Identity, organization_id: uuid.UUID) -> None:
    if actor.organization_id != organization_id:
        raise PermissionDeniedError(
            "Cannot access another organization's data.",
            error_code="users.cross_organization_denied",
            detail={"organization_id": str(organization_id)},
        )


def _require_manage(actor: Identity, organization_id: uuid.UUID) -> None:
    _ensure_same_organization(actor, organization_id)
    users_service.require_permission(actor, ADMIN_PERMISSION)


def _validate_permission_codes(codes: Iterable[str]) -> frozenset[str]:
    wanted = frozenset(codes)
    unknown = sorted(wanted - ASSIGNABLE_PERMISSION_CODES)
    if unknown:
        raise ValidationError(
            "Unknown or non-assignable permission.",
            error_code="role.unknown_permission",
            detail={"permissions": unknown},
        )
    return wanted


def _assert_actor_holds(actor: Identity, codes: Iterable[str]) -> None:
    """No privilege escalation: the actor must themselves hold every code."""
    missing = sorted(set(codes) - set(actor.permissions))
    if missing:
        raise PermissionDeniedError(
            "You cannot grant permissions you do not hold yourself.",
            error_code="role.permission_escalation",
            detail={"permissions": missing},
        )


def _assert_mutable(role: RoleRow) -> None:
    if role.organization_id is None:
        raise PermissionDeniedError(
            "Built-in roles cannot be modified or deleted.",
            error_code="role.builtin_immutable",
            detail={"role_id": str(role.id), "role": role.name},
        )


async def _require_visible_role(
    session: AsyncSession, organization_id: uuid.UUID, role_id: uuid.UUID, *, lock: bool = False
) -> RoleRow:
    role = await role_repository.get_visible_role(session, organization_id, role_id, lock=lock)
    if role is None:  # also covers another organization's role: indistinguishable
        raise NotFoundError(
            "Role not found.", error_code="role.not_found", detail={"role_id": str(role_id)}
        )
    return role


async def _require_mutable_role_locked(
    session: AsyncSession, organization_id: uuid.UUID, role_id: uuid.UUID
) -> RoleRow:
    """A custom role of this organization, row-locked for the rest of the
    transaction. Built-in roles are refused *before* locking: `SELECT ... FOR
    UPDATE` also has to satisfy the UPDATE policy, which (by design) no
    built-in row does, so it would look "not found" instead of "immutable"."""
    row = await _require_visible_role(session, organization_id, role_id)
    _assert_mutable(row)
    locked = await role_repository.get_visible_role(session, organization_id, role_id, lock=True)
    if locked is None:  # deleted concurrently
        raise NotFoundError(
            "Role not found.", error_code="role.not_found", detail={"role_id": str(role_id)}
        )
    return locked


async def _assert_name_free(
    session: AsyncSession, organization_id: uuid.UUID, name: str, *, except_role_id: uuid.UUID | None = None
) -> None:
    existing = await role_repository.find_role_by_name(session, organization_id, name)
    if existing is not None and existing.id != except_role_id:
        raise ConflictError(
            "A role with this name already exists.",
            error_code="role.name_taken",
            detail={"name": name, "builtin": existing.organization_id is None},
        )


async def _assert_org_keeps_admin(
    session: AsyncSession,
    organization_id: uuid.UUID,
    mutate: Callable[[], Awaitable[None]],
) -> None:
    """Run `mutate`, refusing (and undoing it) if it would leave the
    organization with no administrator.

    Takes the per-organization advisory lock first, so two concurrent
    demotions serialize and the second sees the first's result; the mutation
    runs in a savepoint so a refusal leaves no partial change behind. If the
    organization already had no administrator (legacy data) the change cannot
    make that worse and is allowed.
    """
    await repository.acquire_org_admin_lock(session, organization_id)
    before = await repository.list_org_admin_user_ids(session, organization_id, lock=True)
    async with session.begin_nested():
        await mutate()
        after = await repository.list_org_admin_user_ids(session, organization_id, lock=False)
        if before and not after:
            raise ConflictError(
                "This change would leave the organization without an administrator. "
                "Give another user admin access first.",
                error_code="org.last_admin",
                detail={"organization_id": str(organization_id)},
            )


async def _to_role_models(
    session: AsyncSession, organization_id: uuid.UUID, rows: Sequence[RoleRow]
) -> list[Role]:
    codes = await role_repository.permission_codes_by_role(session, [r.id for r in rows])
    counts = await role_repository.member_counts_by_role(session, organization_id)
    return [
        Role(
            id=row.id,
            name=row.name,
            description=row.description,
            is_builtin=row.organization_id is None,
            permissions=codes.get(row.id, ()),
            member_count=counts.get(row.id, 0),
        )
        for row in rows
    ]


async def _one(session: AsyncSession, organization_id: uuid.UUID, row: RoleRow) -> Role:
    return (await _to_role_models(session, organization_id, [row]))[0]


# --- reads --------------------------------------------------------------------


def list_permission_catalog(actor: Identity, organization_id: uuid.UUID) -> list[PermissionInfo]:
    """The assignable permissions with their labels and descriptions. Any
    member of the organization may read it (it contains no tenant data)."""
    _ensure_same_organization(actor, organization_id)
    return [
        PermissionInfo(code=p.code, label=p.label, description=p.description, group=p.group)
        for p in ASSIGNABLE_PERMISSIONS
    ]


async def list_roles(session: AsyncSession, actor: Identity, organization_id: uuid.UUID) -> list[Role]:
    _require_manage(actor, organization_id)
    rows = await role_repository.list_visible_roles(session, organization_id)
    return await _to_role_models(session, organization_id, rows)


async def get_role(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, role_id: uuid.UUID
) -> Role:
    _require_manage(actor, organization_id)
    row = await _require_visible_role(session, organization_id, role_id)
    return await _one(session, organization_id, row)


async def assert_actor_can_grant_role(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, role_id: uuid.UUID
) -> None:
    """Escalation guard for callers that hand a role to someone (invitations,
    access rules, member role changes)."""
    codes = await role_repository.permission_codes_by_role(session, [role_id])
    _assert_actor_holds(actor, codes.get(role_id, ()))


async def resolve_role_id(
    session: AsyncSession, organization_id: uuid.UUID, role_name: str
) -> uuid.UUID:
    """Role *name* -> id among the roles `organization_id` may use (its own
    first, then built-ins). Raises `role.not_found` (422) for an unknown name,
    including another organization's role."""
    role = await role_repository.find_role_by_name(session, organization_id, role_name)
    if role is None:
        raise ValidationError(
            "Unknown role name.", error_code="role.not_found", detail={"role": role_name}
        )
    return role.id


# --- role CRUD ----------------------------------------------------------------


async def create_role(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, data: RoleCreate
) -> Role:
    _require_manage(actor, organization_id)
    codes = _validate_permission_codes(data.permissions)
    _assert_actor_holds(actor, codes)
    await _assert_name_free(session, organization_id, data.name)

    try:
        async with session.begin_nested():
            row = await role_repository.insert_role(
                session,
                organization_id=organization_id,
                name=data.name,
                description=data.description,
            )
    except IntegrityError as exc:  # lost a race with a concurrent create
        raise ConflictError(
            "A role with this name already exists.",
            error_code="role.name_taken",
            detail={"name": data.name},
        ) from exc
    await role_repository.set_role_permission_codes(session, row.id, codes)

    await record_audit_event(
        session,
        actor,
        action="role.create",
        resource_type="role",
        resource_id=row.id,
        metadata={"name": row.name, "permissions": sorted(codes)},
    )
    return await _one(session, organization_id, row)


async def update_role(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    role_id: uuid.UUID,
    data: RoleUpdate,
) -> Role:
    _require_manage(actor, organization_id)
    row = await _require_mutable_role_locked(session, organization_id, role_id)

    fields = data.model_fields_set
    before_codes = frozenset(
        (await role_repository.permission_codes_by_role(session, [row.id])).get(row.id, ())
    )
    new_codes = before_codes
    if "permissions" in fields and data.permissions is not None:
        new_codes = _validate_permission_codes(data.permissions)
        _assert_actor_holds(actor, new_codes - before_codes)  # only *added* codes need holding

    changes: dict[str, object] = {}
    if "name" in fields and data.name is not None and data.name != row.name:
        await _assert_name_free(session, organization_id, data.name, except_role_id=row.id)
        changes["name"] = {"from": row.name, "to": data.name}
    if "description" in fields and data.description != row.description:
        changes["description"] = True
    if new_codes != before_codes:
        changes["permissions"] = {
            "added": sorted(new_codes - before_codes),
            "removed": sorted(before_codes - new_codes),
        }
    if not changes:
        return await _one(session, organization_id, row)

    async def apply() -> None:
        if "name" in changes:
            row.name = data.name  # type: ignore[assignment]
        if "description" in changes:
            row.description = data.description
        if "permissions" in changes:
            await role_repository.set_role_permission_codes(session, row.id, new_codes)
        await session.flush()

    try:
        if ADMIN_PERMISSION in before_codes and ADMIN_PERMISSION not in new_codes:
            await _assert_org_keeps_admin(session, organization_id, apply)
        else:
            async with session.begin_nested():
                await apply()
    except IntegrityError as exc:
        raise ConflictError(
            "A role with this name already exists.",
            error_code="role.name_taken",
            detail={"name": data.name},
        ) from exc

    await record_audit_event(
        session,
        actor,
        action="role.update",
        resource_type="role",
        resource_id=row.id,
        metadata={"name": row.name, "changes": changes},
    )
    return await _one(session, organization_id, row)


async def delete_role(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, role_id: uuid.UUID
) -> None:
    _require_manage(actor, organization_id)
    row = await _require_mutable_role_locked(session, organization_id, role_id)

    usage = await role_repository.role_usage(session, organization_id, row.id)
    if any(usage.values()):
        raise ConflictError(
            "This role is still in use. Remove it from every member, invitation and "
            "access rule before deleting it.",
            error_code="role.in_use",
            detail={"role_id": str(row.id), "usage": usage},
        )

    name = row.name
    await role_repository.delete_role(session, row)
    await record_audit_event(
        session,
        actor,
        action="role.delete",
        resource_type="role",
        resource_id=role_id,
        metadata={"name": name},
    )


# --- member role assignment -------------------------------------------------------


async def _require_member(
    session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    # 404 for a stranger and for a member of another organization alike.
    if not await repository.is_organization_member(session, user_id, organization_id):
        raise NotFoundError(
            "Member not found.", error_code="member.not_found", detail={"user_id": str(user_id)}
        )


async def _member_roles(
    session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID
) -> MemberRoles:
    ids = await role_repository.get_user_role_ids(session, organization_id, user_id)
    rows = await role_repository.get_visible_roles_by_ids(session, organization_id, ids)
    ordered = sorted(rows, key=lambda r: r.name.lower())
    return MemberRoles(
        user_id=user_id, role_ids=tuple(r.id for r in ordered), roles=tuple(r.name for r in ordered)
    )


async def _roles_for_assignment(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, role_ids: Iterable[uuid.UUID]
) -> list[RoleRow]:
    wanted = list(dict.fromkeys(role_ids))
    rows = await role_repository.get_visible_roles_by_ids(session, organization_id, wanted)
    if len(rows) != len(wanted):
        raise NotFoundError("Role not found.", error_code="role.not_found")
    return list(rows)


def _group_managed_error(role_id: uuid.UUID) -> ConflictError:
    return ConflictError(
        "This role is granted by an identity-provider group. Change the member's group membership or the "
        "group's role mapping instead; removing it here would be undone by the next synchronization.",
        error_code="member.role_group_managed",
        detail={"role_id": str(role_id)},
    )


async def add_member_role(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
) -> MemberRoles:
    _require_manage(actor, organization_id)
    await _require_member(session, organization_id, user_id)
    await _roles_for_assignment(session, actor, organization_id, [role_id])
    await assert_actor_can_grant_role(session, actor, organization_id, role_id)

    current = await role_repository.get_user_role_ids(session, organization_id, user_id)
    if role_id in current:
        # An administrator explicitly granting a role the member holds through a group makes it THEIR grant:
        # it becomes `manual`, so withdrawing the group later can never take it away.
        await users_service.assign_role(
            session, user_id=user_id, organization_id=organization_id, role_id=role_id
        )
    if role_id not in current:
        await users_service.assign_role(
            session, user_id=user_id, organization_id=organization_id, role_id=role_id
        )
        await record_audit_event(
            session,
            actor,
            action="member.role_add",
            resource_type="user",
            resource_id=user_id,
            metadata={"role_id": str(role_id)},
        )
    return await _member_roles(session, organization_id, user_id)


async def remove_member_role(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
) -> MemberRoles:
    _require_manage(actor, organization_id)
    await _require_member(session, organization_id, user_id)

    # Lock order matters (deadlock otherwise): the per-organization admin lock
    # is always taken BEFORE any row lock, everywhere -- same as core.privacy.
    await repository.acquire_org_admin_lock(session, organization_id)
    current = await role_repository.get_user_role_ids(session, organization_id, user_id, lock=True)
    if role_id not in current:
        raise NotFoundError(
            "The member does not hold this role.",
            error_code="member.role_not_held",
            detail={"user_id": str(user_id), "role_id": str(role_id)},
        )
    if role_id in await role_repository.group_managed_role_ids(session, organization_id, user_id):
        raise _group_managed_error(role_id)
    if len(current) == 1:
        raise ConflictError(
            "A member must keep at least one role. Assign another role before removing this one.",
            error_code="member.last_role",
            detail={"user_id": str(user_id)},
        )

    async def apply() -> None:
        await role_repository.delete_user_role(session, organization_id, user_id, role_id)

    await _assert_org_keeps_admin(session, organization_id, apply)
    await record_audit_event(
        session,
        actor,
        action="member.role_remove",
        resource_type="user",
        resource_id=user_id,
        metadata={"role_id": str(role_id)},
    )
    return await _member_roles(session, organization_id, user_id)


async def set_member_roles(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    role_ids: Sequence[uuid.UUID],
) -> MemberRoles:
    """Replace the member's complete role set (at least one role)."""
    _require_manage(actor, organization_id)
    await _require_member(session, organization_id, user_id)
    wanted_rows = await _roles_for_assignment(session, actor, organization_id, role_ids)
    wanted = {r.id for r in wanted_rows}

    # Admin lock before any row lock -- see remove_member_role.
    await repository.acquire_org_admin_lock(session, organization_id)
    current = await role_repository.get_user_role_ids(session, organization_id, user_id, lock=True)
    to_add = wanted - current
    to_remove = current - wanted
    if not to_add and not to_remove:
        return await _member_roles(session, organization_id, user_id)

    managed = await role_repository.group_managed_role_ids(session, organization_id, user_id)
    for role_id in to_remove & managed:
        raise _group_managed_error(role_id)

    for role_id in to_add:  # only newly granted roles need the escalation check
        await assert_actor_can_grant_role(session, actor, organization_id, role_id)

    async def apply() -> None:
        for role_id in sorted(to_add, key=str):
            await users_service.assign_role(
                session, user_id=user_id, organization_id=organization_id, role_id=role_id
            )
        for role_id in sorted(to_remove, key=str):
            await role_repository.delete_user_role(session, organization_id, user_id, role_id)

    if to_remove:
        await _assert_org_keeps_admin(session, organization_id, apply)
    else:
        await apply()

    await record_audit_event(
        session,
        actor,
        action="member.roles_set",
        resource_type="user",
        resource_id=user_id,
        metadata={
            "added": sorted(str(r) for r in to_add),
            "removed": sorted(str(r) for r in to_remove),
        },
    )
    return await _member_roles(session, organization_id, user_id)


# --- starter roles -------------------------------------------------------------------


async def ensure_default_roles(session: AsyncSession, organization_id: uuid.UUID) -> list[uuid.UUID]:
    """Create the starter roles (`viewer`, `engineer`, `reviewer`) for an
    organization that lacks them; never touches one that already exists (an
    administrator may have edited it). The caller must already have set the
    tenant context to `organization_id` (row-level security)."""
    created: list[uuid.UUID] = []
    for template in DEFAULT_ROLE_TEMPLATES:
        existing = await role_repository.find_role_by_name(session, organization_id, template.name)
        if existing is not None:
            continue
        row = await role_repository.insert_role(
            session,
            organization_id=organization_id,
            name=template.name,
            description=template.description,
        )
        await role_repository.set_role_permission_codes(session, row.id, template.permissions)
        created.append(row.id)
    if created:
        logger.info(
            "default_roles_created", organization_id=str(organization_id), count=len(created)
        )
    return created
