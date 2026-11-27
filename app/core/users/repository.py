"""This module handles database access for users, roles, and permissions. Each function 
performs a query and returns data, while `service.py` handles identity creation and 
authorization decisions. The key optimization is that `get_role_names` and 
`get_permission_codes` fetch the required information in just two indexed queries, 
allowing later permission checks to happen in memory without additional database calls. 
For multi-tenancy, both queries filter by `user_id` and `organization_id`, ensuring a 
user's roles and permissions are resolved only for the current organization, preventing 
cross-company permission leaks.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from datetime import datetime

from sqlalchemy import exists, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.users.permissions import ASSIGNABLE_PERMISSIONS
from app.database.models.core_models import (
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)
from app.database.models.tenancy_models import (
    Organization,
    OrganizationMemberState,
    Project,
    ProjectMembership,
)

# The permission list the built-in admin role carries. Derived from the
# assignable-permission catalog (`core.users.permissions`) so the two can never
# drift apart; order is not significant.
ADMIN_PERMISSION_CODES: Sequence[str] = tuple(spec.code for spec in ASSIGNABLE_PERMISSIONS)


async def get_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Fetch a single user by primary key, or None if absent."""
    return await session.get(User, user_id)


async def get_organization_status(
    session: AsyncSession, organization_id: uuid.UUID
) -> tuple[bool, str | None]:
    """`(exists, status)` of an organization -- one primary-key read of ONE column.

    `organizations` carries no row-level security (it IS the tenant boundary), so this needs no
    tenant context. Used by `resolve_identity`, which runs on every authenticated request.
    """
    result = await session.execute(
        select(Organization.status).where(Organization.id == organization_id)
    )
    row = result.first()
    return (row is not None, row[0] if row is not None else None)


async def get_organization_access_state(
    session: AsyncSession, organization_id: uuid.UUID
) -> tuple[bool, str | None, int]:
    """`(exists, status, session_epoch)` of an organization -- one primary-key read of two columns.

    The session epoch is the organization-level session version: access tokens carry the value they
    were issued under and are refused once it moved (every suspension and reactivation moves it).
    Same properties as `get_organization_status`: no tenant context needed, a column query so it is
    never served from the identity map.
    """
    result = await session.execute(
        select(Organization.status, Organization.session_epoch).where(Organization.id == organization_id)
    )
    row = result.first()
    if row is None:
        return (False, None, 0)
    return (True, row[0], int(row[1] or 0))


async def get_organization_suspension_notice(
    session: AsyncSession, organization_id: uuid.UUID
) -> dict[str, str | None]:
    """The customer-facing facts shown to someone refused because their organization is suspended:
    its name and the operator's customer message (NEVER the reason or the private operator note, which
    live in a table this role cannot read). Only called on the already-refusing path."""
    row = (
        await session.execute(
            select(Organization.name, Organization.status_message_public).where(
                Organization.id == organization_id
            )
        )
    ).first()
    if row is None:
        return {}
    return {"organization_name": row[0], "customer_message": row[1]}


async def get_member_state(
    session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID
) -> OrganizationMemberState | None:
    """The member's state row, or None (= active, never deactivated). `organization_member_states` is
    FORCE-RLS: the caller must already have set `organization_id` as the tenant context."""
    return (
        await session.execute(
            select(OrganizationMemberState)
            .where(
                OrganizationMemberState.organization_id == organization_id,
                OrganizationMemberState.user_id == user_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def get_member_access_state(
    session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[bool, int]:
    """`(deactivated, member_epoch)` -- ONE primary-key read of two columns, never the identity map (a
    long-running request must see a deactivation committed a moment ago). No row = `(False, 0)`.
    Needs the tenant context to be `organization_id` (row-level security)."""
    row = (
        await session.execute(
            select(OrganizationMemberState.status, OrganizationMemberState.member_epoch).where(
                OrganizationMemberState.organization_id == organization_id,
                OrganizationMemberState.user_id == user_id,
            )
        )
    ).first()
    if row is None:
        return (False, 0)
    return (row[0] == "deactivated", int(row[1]))


async def lock_member_state(
    session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID
) -> OrganizationMemberState | None:
    """The state row, locked FOR UPDATE until the transaction ends (None when there is no row)."""
    return (
        await session.execute(
            select(OrganizationMemberState)
            .where(
                OrganizationMemberState.organization_id == organization_id,
                OrganizationMemberState.user_id == user_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def set_member_state(
    session: AsyncSession,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    status: str,
    source: str,
    reason: str | None,
) -> OrganizationMemberState:
    """Create or update the state row. The DATABASE computes `member_epoch`, `deactivated_at` and
    `updated_at` (trigger `organization_member_state_guard`); nothing here can set them."""
    existing = await lock_member_state(session, organization_id, user_id)
    if existing is None:
        row = OrganizationMemberState(
            organization_id=organization_id, user_id=user_id, status=status, source=source, reason=reason
        )
        session.add(row)
    else:
        existing.status = status
        existing.source = source
        existing.reason = reason
        row = existing
    await session.flush()
    await session.refresh(row)
    return row


async def list_deactivated_organization_ids(
    session: AsyncSession, user_id: uuid.UUID
) -> set[uuid.UUID]:
    """Organizations where `user_id` is deactivated, through the narrow SECURITY DEFINER lookup (the
    login / selection / switching paths run before any tenant context exists, so RLS would hide every
    row). Returns organization ids only."""
    result = await session.execute(
        text("SELECT member_deactivated_organization_ids(:user_id)"), {"user_id": user_id}
    )
    return {row[0] for row in result.all()}


async def get_member_deactivation_sources(
    session: AsyncSession, organization_id: uuid.UUID
) -> dict[uuid.UUID, str]:
    """`{user_id: source}` for every member currently deactivated in this organization (tenant context set)."""
    rows = await session.execute(
        select(OrganizationMemberState.user_id, OrganizationMemberState.source).where(
            OrganizationMemberState.organization_id == organization_id,
            OrganizationMemberState.status == "deactivated",
        )
    )
    return {r[0]: r[1] for r in rows.all()}


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    """Fetch a single user by their unique email, or None if absent.

    Used by core/auth during credential resolution (login looks a user up by
    email before issuing a token).
    """
    stmt = select(User).where(User.email == email)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_by_email_ci(session: AsyncSession, email: str) -> User | None:
    """Case-insensitive "does an account with this address exist" lookup.

    `users.email` is a case-sensitive UNIQUE column and legacy rows can be
    mixed-case, so an exact match would let `Alice@x.com` and `alice@x.com`
    both exist. Every NEW identity is stored lower-cased; this lookup is what
    the signup/verification/linking paths use so they can never create a
    lower-cased twin of a legacy row. Deterministic if (legacy) duplicates
    exist: exact match first, then oldest.
    """
    stmt = (
        select(User)
        .where(func.lower(User.email) == email.strip().lower())
        .order_by((User.email == email).desc(), User.created_at.asc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def mark_email_verified(
    session: AsyncSession, *, user_id: uuid.UUID, verified_at: datetime, via: str
) -> None:
    row = await session.get(User, user_id)
    if row is None:
        raise ValueError(f"mark_email_verified: no such user {user_id}")
    row.email_verified_at = verified_at
    row.email_verified_via = via
    await session.flush()


async def list_organization_members(
    session: AsyncSession, organization_id: uuid.UUID
) -> Sequence[tuple[User, Sequence[str]]]:
    """Return every user holding at least one role in `organization_id`,
    each paired with the names of every role they hold there -- the read
    side of "who is on this team," backing the previously-missing `GET
    /organizations/{id}/members` (the frontend's Users settings page called
    a `GET /users` endpoint that never existed; this is the real
    equivalent).

    Two queries, not a single query with an aggregate: `user_roles` is a
    many-to-many join (a user can hold more than one role in the same
    organization), and building the per-user role-name list in Python next
    to a single ordered `users` query is simpler and just as cheap at this
    scale as a `GROUP BY`/`array_agg`, matching `get_permission_codes`'s own
    "group in Python, not SQL" precedent in `get_project_permission_map`
    just below.
    """
    member_ids_stmt = (
        select(UserRole.user_id).where(UserRole.organization_id == organization_id).distinct()
    )
    member_ids = (await session.execute(member_ids_stmt)).scalars().all()
    if not member_ids:
        return []

    users_stmt = select(User).where(User.id.in_(member_ids)).order_by(User.display_name)
    users = (await session.execute(users_stmt)).scalars().all()

    roles_stmt = (
        select(UserRole.user_id, Role.name)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.organization_id == organization_id, UserRole.user_id.in_(member_ids))
    )
    roles_by_user: dict[uuid.UUID, list[str]] = {}
    for user_id, role_name in (await session.execute(roles_stmt)).all():
        roles_by_user.setdefault(user_id, []).append(role_name)

    return [(user, sorted(roles_by_user.get(user.id, []))) for user in users]


async def get_role_names(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> Sequence[str]:
    """Return the names of the roles assigned to `user_id` within
    `organization_id`.

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


async def insert_user(
    session: AsyncSession,
    *,
    email: str,
    display_name: str,
    password_hash: str | None = None,
    email_verified_at: datetime | None = None,
    email_verified_via: str | None = None,
) -> User:
    """Create a new user row and return it with server defaults populated.

    Called only after a decision to allow provisioning has already been made
    elsewhere (core/tenancy's `evaluate_provisioning`) -- this function
    performs no authorization or policy check of its own; core/users manages
    identity/roles, not who may join an organization.
    """
    row = User(
        email=email,
        display_name=display_name,
        password_hash=password_hash,
        email_verified_at=email_verified_at,
        email_verified_via=email_verified_via,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_role_by_name(
    session: AsyncSession, name: str, organization_id: uuid.UUID | None = None
) -> Role | None:
    """Fetch a role by name (case-insensitive), or None if absent.

    With `organization_id`, resolves among the roles that organization can use:
    its own custom roles first, then the built-ins. Without it, only built-in
    (platform-wide) roles are considered -- a custom role is never resolved
    outside the organization that owns it (and row-level security hides it
    anyway). Used by core/tenancy to turn a role name supplied on an
    `AccessRuleCreate`/`InvitationCreate` request into the stored
    `grants_role_id`.
    """
    cleaned = name.strip().lower()
    if organization_id is not None:
        stmt = (
            select(Role)
            .where(
                func.lower(Role.name) == cleaned,
                or_(Role.organization_id.is_(None), Role.organization_id == organization_id),
            )
            .order_by(Role.organization_id.is_(None))  # the organization's own role first
            .limit(1)
        )
    else:
        stmt = select(Role).where(func.lower(Role.name) == cleaned, Role.organization_id.is_(None))
    return (await session.execute(stmt)).scalar_one_or_none()


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
    granted_via: str = "manual",
) -> None:
    """Assign `role_id` to `user_id` within `organization_id`. `granted_via` is the role's provenance:
    `manual` (the default) or `scim` (created by group synchronization, the only kind it may later remove).

    Not idempotent by itself -- inserting a duplicate composite key raises an
    integrity error. Callers are expected to check `get_user_role` first
    (the same "check, then act" pattern used elsewhere in this module, e.g.
    `resolve_identity`'s `is_active` check) rather than this function
    silently swallowing a conflict.
    """
    session.add(
        UserRole(user_id=user_id, organization_id=organization_id, role_id=role_id, granted_via=granted_via)
    )
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


async def get_project_permission_map(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> dict[uuid.UUID, frozenset[str]]:

    """Return a user's permission codes grouped by project within an organization."""

    stmt = (
        select(ProjectMembership.project_id, Permission.code)
        .join(Project, Project.id == ProjectMembership.project_id)
        .join(RolePermission, RolePermission.role_id == ProjectMembership.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(
            ProjectMembership.user_id == user_id,
            Project.organization_id == organization_id,
        )
        .distinct()
    )
    result = await session.execute(stmt)

    grouped: dict[uuid.UUID, set[str]] = {}
    for project_id, code in result.all():
        grouped.setdefault(project_id, set()).add(code)
    return {project_id: frozenset(codes) for project_id, codes in grouped.items()}


async def update_password_hash(session: AsyncSession, *, user_id: uuid.UUID, password_hash: str) -> None:
    """Set (or replace) `user_id`'s local password credential.

    Used only by `core.auth.service.signup` -- keeps the "only core/users's
    repository writes `users` rows" discipline intact rather than having
    core/auth reach into this table directly.
    """
    row = await session.get(User, user_id)
    if row is None:
        raise ValueError(f"update_password_hash: no such user {user_id}")  # unreachable in practice
    row.password_hash = password_hash
    await session.flush()


async def replace_password_hash(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    password_hash: str,
    sessions_valid_after: datetime,
) -> bool:
    """Set a NEW credential and the session cut-off in one statement (row-locked
    by the UPDATE itself). Returns False if no such user."""
    result = await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(password_hash=password_hash, sessions_valid_after=sessions_valid_after)
    )
    await session.flush()
    return bool(result.rowcount)


async def upgrade_password_hash(
    session: AsyncSession, *, user_id: uuid.UUID, old_hash: str, new_hash: str
) -> bool:
    """Swap one hash of the SAME password for a stronger one, only if the stored hash
    is still `old_hash`. A password reset/change that landed in between wins (this
    returns False and changes nothing), and the session cut-off is never touched."""
    result = await session.execute(
        update(User)
        .where(User.id == user_id, User.password_hash == old_hash)
        .values(password_hash=new_hash)
    )
    await session.flush()
    return bool(result.rowcount)


async def get_first_organization_id(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    """Return an organization where the user has a role, or None if they have none.
Used during password login when the user's organization is not yet known.
Because Row-Level Security blocks normal queries before tenant context exists,
it uses a dedicated SQL function to safely resolve the organization. Signup
currently assigns each password-auth user exactly one organization role.
"""

    result = await session.execute(
        text("SELECT resolve_user_first_organization(:user_id)"),
        {"user_id": str(user_id)},
    )
    return result.scalar_one_or_none()


async def list_organization_ids(session: AsyncSession, user_id: uuid.UUID) -> Sequence[uuid.UUID]:
    """Return every organization `user_id` holds a role in (possibly empty).

    The generalized sibling of `get_first_organization_id` above, added for
    the multi-organization login/session design gap (`f6a7b8c9d0e1_list_
    user_organizations_function.py`): a password login needs to tell
    "exactly one organization" apart from "more than one" *before* deciding
    whether to log the user straight in or require them to pick, which
    `get_first_organization_id`'s `LIMIT 1`-shaped result can't answer by
    itself. Deliberately does NOT replace or change `get_first_organization_id`
    -- both remain, each used where its own shape is what's needed.

    Same RLS bootstrap problem as `get_first_organization_id` (no tenant
    context can exist yet at this point in the flow, and `user_roles` is
    `FORCE ROW LEVEL SECURITY`), so this goes through the equally narrow
    `list_user_organization_ids` SQL function rather than a plain ORM query
    against `UserRole`, mirroring `app.ingestion.repository.
    list_active_connector_config_ids`'s exact multi-row bypass-function
    pattern.
    """
    result = await session.execute(
        text("SELECT organization_id FROM list_user_organization_ids(:user_id)"),
        {"user_id": str(user_id)},
    )
    return result.scalars().all()


async def is_organization_member(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> bool:
    """Whether `user_id` *currently* holds at least one role in
    `organization_id` -- EKIP's definition of "current member" until a
    dedicated membership table exists.

    Filters by organization explicitly even though `user_roles` is also
    RLS-protected: the application-level filter is the primary check and RLS
    is the backstop, same as every other org-scoped query in this module.
    """
    stmt = (
        select(func.count())
        .select_from(UserRole)
        .where(UserRole.user_id == user_id, UserRole.organization_id == organization_id)
    )
    return int((await session.execute(stmt)).scalar_one()) > 0


#: Permission code that defines "organization admin" (see `core.tenancy.
#: service`'s module docstring: admin-ness is holding this code, not a flag).
_ADMIN_PERMISSION_CODE = "tenancy:manage"

#: First key of the two-key advisory lock below. An arbitrary constant that
#: only needs to differ from any other advisory-lock namespace in the codebase.
_ORG_ADMIN_LOCK_NAMESPACE = 7001


async def acquire_org_admin_lock(session: AsyncSession, organization_id: uuid.UUID) -> None:
    """Take a transaction-scoped advisory lock serializing every operation
    that could shrink `organization_id`'s set of admins.

    Released automatically at commit/rollback (`pg_advisory_xact_lock`), so it
    is safe with pooled connections. Any code path that removes or demotes an
    admin MUST take this lock, then re-read the admin set, then decide --
    see `core.users.service.assert_organization_keeps_an_admin`. A bare row
    lock is not enough on its own: it makes a concurrent writer wait, but the
    writer then proceeds on a stale decision unless it re-checks after
    acquiring the same lock.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, hashtext(:organization_id))"),
        {"namespace": _ORG_ADMIN_LOCK_NAMESPACE, "organization_id": str(organization_id)},
    )


async def list_org_admin_user_ids(
    session: AsyncSession, organization_id: uuid.UUID, *, lock: bool
) -> set[uuid.UUID]:
    """Ids of the users who can currently act as admins of `organization_id`:
    they hold a role granting `tenancy:manage` there, and their account is
    active and email-verified (an account `resolve_identity` would reject
    cannot exercise admin power, so it must not be counted as a safety net).

    With `lock=True` the matching `user_roles` rows are also locked
    (`FOR UPDATE OF user_roles`), so a concurrent transaction that tries to
    delete or change one of them waits for this one to finish. Ordered by
    `user_id` so two lockers always acquire row locks in the same order. The
    result is de-duplicated in Python, not with `DISTINCT`, because Postgres
    rejects `FOR UPDATE` combined with `DISTINCT`.
    """
    stmt = (
        select(UserRole.user_id)
        .join(RolePermission, RolePermission.role_id == UserRole.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .join(User, User.id == UserRole.user_id)
        .where(
            UserRole.organization_id == organization_id,
            Permission.code == _ADMIN_PERMISSION_CODE,
            User.is_active.is_(True),
            User.email_verified_at.is_not(None),
            # A deactivated member cannot exercise admin power here, so they are no safety net either.
            ~exists().where(
                OrganizationMemberState.organization_id == UserRole.organization_id,
                OrganizationMemberState.user_id == UserRole.user_id,
                OrganizationMemberState.status == "deactivated",
            ),
        )
        .order_by(UserRole.user_id)
    )
    if lock:
        stmt = stmt.with_for_update(of=UserRole)
    return set((await session.execute(stmt)).scalars().all())


async def lock_user_row(
    session: AsyncSession, user_id: uuid.UUID, *, exclusive: bool
) -> bool | None:
    """Lock the global `users` row of `user_id` until the transaction ends and
    return its current `is_active`, or `None` if no such user exists.

    `exclusive=True` is `FOR UPDATE` (account deletion/anonymization);
    `exclusive=False` is `FOR SHARE` (granting a role). The two conflict, so
    a role grant and an account anonymization for the same person are
    serialized, and -- Postgres re-reads the row after waiting -- whichever
    runs second sees the first one's committed result (a grant after an
    anonymization reads `is_active=False`).
    """
    stmt = select(User.is_active).where(User.id == user_id).with_for_update(read=not exclusive)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_or_create_permissions(session: AsyncSession, codes: Sequence[str]) -> list[Permission]:
    """Fetch existing `Permission` rows for `codes`, inserting any missing
    ones -- the same "populate the fixed catalog if it isn't there yet"
    behavior `scripts/seed_test_organization.py`'s dev bootstrap already
    relies on, now reusable by real signup instead of only a dev script.
    """
    result = await session.execute(select(Permission).where(Permission.code.in_(codes)))
    existing = {row.code: row for row in result.scalars().all()}

    permissions = []
    for code in codes:
        if code in existing:
            permissions.append(existing[code])
            continue
        permission = Permission(code=code, description=f"Seeded by signup bootstrap: {code}")
        session.add(permission)
        permissions.append(permission)
    await session.flush()
    return permissions


async def get_or_create_role_by_name(session: AsyncSession, name: str, *, description: str) -> Role:
    """Fetch the `Role` named `name`, creating it (with `description`) if it
    doesn't exist yet. Idempotent -- safe to call on every signup, not just
    the first.
    """
    existing = await get_role_by_name(session, name)
    if existing is not None:
        return existing

    role = Role(name=name, description=description)  # organization_id NULL: a built-in role
    session.add(role)
    await session.flush()
    return role


async def get_missing_permission_codes(
    session: AsyncSession, role_id: uuid.UUID, codes: Sequence[str]
) -> list[str]:
    """Which of `codes` the role does not yet grant (read-only)."""
    granted = await session.execute(
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role_id)
    )
    have = set(granted.scalars().all())
    return [code for code in codes if code not in have]


async def grant_permissions_to_role(
    session: AsyncSession, *, role_id: uuid.UUID, permissions: Sequence[Permission]
) -> None:
    """Grant every permission in `permissions` to `role_id`, skipping any
    already granted -- idempotent, mirroring `insert_user_role`'s sibling
    `get_user_role`-then-`insert_user_role` pattern rather than relying on a
    database-constraint-driven retry.
    """
    granted = await session.execute(
        select(RolePermission.permission_id).where(RolePermission.role_id == role_id)
    )
    already_granted = set(granted.scalars().all())
    for permission in permissions:
        if permission.id not in already_granted:
            session.add(RolePermission(role_id=role_id, permission_id=permission.id))
    await session.flush()
