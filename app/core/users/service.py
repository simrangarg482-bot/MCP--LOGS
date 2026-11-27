"""This is the central authority for user identity and permissions in EKIP. 
`resolve_identity` loads a user's roles and permissions for a specific organization and 
builds their `Identity`; it is the only approved way to create one. Once the identity 
is loaded, `authorize()` and `require_permission()` check permissions quickly in memory, 
ensuring REST APIs and MCP tools follow the same access-control rules. The module also 
supports project-level permissions: when a `project_id` is provided, it checks 
permissions for that project; otherwise, it performs the organization-level check. 
`require_project_permission()` is simply a clearer wrapper for operations that always 
require project-level authorization.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.core.users import repository, role_repository
from app.core.users.permissions import BUILTIN_ADMIN_ROLE_DESCRIPTION
from app.core.users.schemas import (
    OrganizationMember,
    UserCredentialLookup,
    UserEmailState,
    UserProfile,
)
from app.database.session import set_tenant_context
from app.shared.config.logging import get_logger
from app.shared.organization_status import is_recognized, is_suspended
from app.shared.schemas import ActorKind, Identity

logger = get_logger(__name__)


def _ensure_same_organization(actor: Identity, organization_id: uuid.UUID) -> None:
    """Tenant-isolation guard, same shape as `core.tenancy.service`'s own
    (duplicated per that module's own precedent -- see its docstring on why
    a small, cheap guard like this is copied per-module rather than shared).
    Needed here because `list_organization_members`, unlike every other
    function in this file, is reached via an explicit `{organization_id}`
    path parameter (`GET /organizations/{id}/members`), not derived purely
    from `actor.organization_id` with no override possible.
    """
    if actor.organization_id != organization_id:
        raise PermissionDeniedError(
            "Cannot access another organization's data.",
            error_code="users.cross_organization_denied",
            detail={"organization_id": str(organization_id)},
        )


MEMBERSHIP_DEACTIVATED_CODE = "auth.membership_deactivated"
MEMBERSHIP_DEACTIVATED_MESSAGE = (
    "Your access to this organization has been deactivated. Contact your administrator."
)


def membership_deactivated_error() -> PermissionDeniedError:
    """The one refusal for a member whose access to THIS organization was deactivated (REST, MCP, login,
    refresh, selection, switching and SSO all raise exactly this)."""
    return PermissionDeniedError(MEMBERSHIP_DEACTIVATED_MESSAGE, error_code=MEMBERSHIP_DEACTIVATED_CODE)


async def resolve_identity(
    session: AsyncSession,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    *,
    token_issued_at: datetime | None = None,
    token_org_epoch: int | None = None,
    token_member_epoch: int | None = None,
) -> Identity:
    """This function builds a fully populated `Identity` for a user within a specific 
    organization by loading their user record, roles, and permissions. It raises 
    `NotFoundError` if the user doesn't exist and `PermissionDeniedError` if their 
    account is disabled. If the user has no roles in that organization, it returns an 
    identity with empty permissions, so authorization checks safely deny access. An 
    important security detail: because role and permission tables are protected by 
    Row-Level Security (RLS), the function must set the organization context before 
    querying them; otherwise, valid permissions could appear empty. The authentication 
    module calls this function after verifying the user's credentials and determining 
    their organization.
    """
    await set_tenant_context(session, organization_id)

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
    # The single choke point for already-issued access tokens (REST and MCP
    # both resolve through here on every request): an account whose email
    # ownership has not been established has no authenticated access, even
    # with a validly signed, unexpired token. Zero extra queries -- the row
    # is already loaded.
    if user.email_verified_at is None:
        raise PermissionDeniedError(
            "This account's email address has not been verified.",
            error_code="auth.email_not_verified",
        )

    # A password reset/change stamps `sessions_valid_after`: any access token issued
    # before that instant is refused immediately, whatever its remaining lifetime.
    # `token_issued_at` carries the token's microsecond `iat_us` (or, for a token
    # minted before that claim existed, the START of its `iat` second -- so a
    # same-second pre-reset token is refused, never accepted). Zero extra queries.
    if (
        token_issued_at is not None
        and user.sessions_valid_after is not None
        and token_issued_at < user.sessions_valid_after
    ):
        raise PermissionDeniedError(
            "This session was ended because the password changed. Sign in again.",
            error_code="auth.session_revoked",
        )

    # THE central organization-status check. Every authenticated REST request, every MCP tool
    # call and the MCP authorization step resolve their caller here, so a suspended organization
    # is refused for already-issued access tokens at once -- not after they expire. It is scoped
    # to THIS organization: the same user's token for another organization resolves normally.
    # One primary-key read of one column; `organizations` has no RLS.
    exists, org_status, org_epoch = await repository.get_organization_access_state(
        session, organization_id
    )
    if not exists:
        raise NotFoundError(
            "Organization not found.",
            error_code="organization.not_found",
            detail={"organization_id": str(organization_id)},
        )
    if is_suspended(org_status):
        raise PermissionDeniedError(
            "This organization's access has been suspended. Contact your administrator.",
            error_code="auth.organization_suspended",
            detail=await repository.get_organization_suspension_notice(session, organization_id),
        )
    if not is_recognized(org_status):
        # The database CHECK makes this unreachable for new data; kept as a loud, non-blocking
        # signal rather than locking a customer out over an unexpected value.
        logger.warning("organization_status_unrecognized", organization_id=str(organization_id))
    # The organization session epoch moves on every suspension AND every reactivation, so an access
    # token issued before a suspension stays dead after the organization is reactivated: the user
    # signs in again. A token minted before the claim existed carries epoch 0 (== the value every
    # organization has until its first suspension, so existing sessions are unaffected). A counter,
    # not a timestamp: nothing is compared across machine clocks. Same read as the status above.
    if token_org_epoch is not None and token_org_epoch != org_epoch:
        raise PermissionDeniedError(
            "This session was ended because the organization's access changed. Sign in again.",
            error_code="auth.session_revoked",
        )

    # THIS person's membership of THIS organization (organization-scoped offboarding). A deactivated member
    # is refused on the very next request, and the member epoch -- which moves on every deactivation AND
    # every reactivation -- ends every token issued before it, so reactivation cannot revive them either.
    # A token minted before the claim existed carries epoch 0 (= a member who was never deactivated).
    # One primary-key read; the tenant context for this organization was set at the top of this function.
    member_deactivated, member_epoch = await repository.get_member_access_state(
        session, organization_id, user_id
    )
    if member_deactivated:
        raise membership_deactivated_error()
    if token_member_epoch is not None and token_member_epoch != member_epoch:
        raise PermissionDeniedError(
            "This session was ended because your access to the organization changed. Sign in again.",
            error_code="auth.session_revoked",
        )

    role_names = await repository.get_role_names(session, user_id, organization_id)
    permission_codes = await repository.get_permission_codes(
        session, user_id, organization_id
    )
    project_permissions = await repository.get_project_permission_map(
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
        project_permissions=project_permissions,
    )


async def get_display_name(session: AsyncSession, user_id: uuid.UUID) -> str | None:
    """A user's display name, or None -- e.g. to say who sent an invitation."""
    user = await repository.get_by_id(session, user_id)
    return user.display_name if user is not None else None


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


async def list_organization_members(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID
) -> list[OrganizationMember]:
    """List every user holding a role in `organization_id`, with their role
    names -- backs `GET /organizations/{id}/members`.

    No `require_permission` gate beyond organization membership itself:
    seeing your own teammates' names, emails, and roles is ordinary within
    an organization (needed for e.g. deciding who to loop into an incident),
    unlike `core.tenancy.service.list_invitations`/`list_access_rules`
    """
    _ensure_same_organization(actor, organization_id)
    rows = await repository.list_organization_members(session, organization_id)
    role_ids = await role_repository.get_member_role_ids(session, organization_id)
    deactivated = await repository.get_member_deactivation_sources(session, organization_id)
    return [
        OrganizationMember(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            is_active=user.is_active,
            membership_status="deactivated" if user.id in deactivated else "active",
            membership_source=deactivated.get(user.id),
            roles=tuple(role_names),
            role_ids=role_ids.get(user.id, ()),
            created_at=user.created_at,
        )
        for user, role_names in rows
    ]


async def get_or_create_user(
    session: AsyncSession,
    *,
    email: str,
    display_name: str,
    email_verified_via: str | None = None,
) -> uuid.UUID:
    """Resolve `email` to a user, creating one if none exists yet.

    TRUSTED-BOOTSTRAP ONLY (seed/e2e/operator scripts): this performs no
    ownership check. Request paths must NOT call it with an email taken from
    a request or an IdP claim -- they use `create_verified_user` (after a
    consumed verification token) or `resolve_user_for_sso_provisioning`.
    `email_verified_via` stamps a newly created row as verified (only ever
    `"operator_attestation"` from a script); an existing row is returned
    unchanged.

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

    created = await repository.insert_user(
        session,
        email=email,
        display_name=display_name,
        email_verified_at=datetime.now(timezone.utc) if email_verified_via else None,
        email_verified_via=email_verified_via,
    )
    logger.info("user_provisioned", user_id=str(created.id), email=email)
    return created.id


async def find_user_id_by_email_ci(session: AsyncSession, email: str) -> uuid.UUID | None:
    """Case-insensitive existence check used by signup/verification/linking."""
    row = await repository.get_by_email_ci(session, email)
    return row.id if row is not None else None


def _email_state(row) -> UserEmailState:
    return UserEmailState(
        user_id=row.id,
        email=row.email,
        email_verified_at=row.email_verified_at,
        is_active=row.is_active,
        has_password=row.password_hash is not None,
    )


async def get_user_email_state(session: AsyncSession, email: str) -> UserEmailState | None:
    """Email-ownership view of the user with `email` (case-insensitive), or
    None -- the narrow shape the verification service needs, never an ORM row."""
    row = await repository.get_by_email_ci(session, email)
    return _email_state(row) if row is not None else None


async def get_user_email_state_by_id(
    session: AsyncSession, user_id: uuid.UUID
) -> UserEmailState | None:
    row = await repository.get_by_id(session, user_id)
    return _email_state(row) if row is not None else None


async def create_verified_user(
    session: AsyncSession,
    *,
    email: str,
    display_name: str,
    password_hash: str | None,
    via: str,
) -> uuid.UUID:
    """Create a brand-new user whose email ownership has JUST been proven
    (consumed verification token, or an IdP that affirmatively verified it).
    Refuses if any account (case-insensitive) already uses the address -- it
    never adopts an existing row, which is the squatting/takeover shape this
    change exists to remove.
    """
    if await repository.get_by_email_ci(session, email) is not None:
        raise ConflictError(
            "An account with this email already exists.",
            error_code="auth.email_taken",
        )
    created = await repository.insert_user(
        session,
        email=email.strip().lower(),
        display_name=display_name,
        password_hash=password_hash,
        email_verified_at=datetime.now(timezone.utc),
        email_verified_via=via,
    )
    logger.info("user_provisioned", user_id=str(created.id), via=via)
    return created.id


async def mark_email_verified(session: AsyncSession, *, user_id: uuid.UUID, via: str) -> None:
    await repository.mark_email_verified(
        session, user_id=user_id, verified_at=datetime.now(timezone.utc), via=via
    )


async def resolve_user_for_sso_provisioning(
    session: AsyncSession, *, email: str, display_name: str
) -> uuid.UUID:
    """User to attach a NEW SSO identity mapping to, given an email the IdP
    has *affirmatively verified* (the caller guarantees that).

    * no account with that address -> a new user, created already-verified
      (`via="oidc"`), with no local password;
    * an account exists and its own `email_verified_at` is set -> that
      account (we have independent proof its owner controls the address);
    * an account exists but is unverified (legacy / never proven) ->
      `PermissionDeniedError`. Linking a fresh IdP identity to an account
      whose ownership was never established would hand that account (and
      whatever sessions/passwords a squatter holds on it) to a new login path.
      The owner must first verify through the emailed link.
    """
    existing = await repository.get_by_email_ci(session, email)
    if existing is None:
        return await create_verified_user(
            session, email=email, display_name=display_name, password_hash=None, via="oidc"
        )
    if existing.email_verified_at is None:
        raise PermissionDeniedError(
            "An account with this email exists but its address has not been verified. "
            "Verify it via the emailed link, then sign in again.",
            error_code="auth.account_email_unverified",
        )
    return existing.id


async def holds_any_role_in_organization(
    session: AsyncSession, *, user_id: uuid.UUID, organization_id: uuid.UUID
) -> bool:
    """Whether `user_id` currently holds at least one role in `organization_id`
    -- i.e. is still a member. Reads `user_roles` under the caller's tenant
    context (which must already be `organization_id`'s)."""
    return bool(await role_repository.get_user_role_ids(session, organization_id, user_id))


async def assign_role(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    role_id: uuid.UUID,
    granted_via: str = "manual",
) -> None:
    """Grant `role_id` to `user_id` within `organization_id`, idempotently. `granted_via` records provenance:
    only SCIM group synchronization passes `scim`; every other caller (administrators, invitations, access
    rules) is `manual`. A MANUAL grant of a role that SCIM had created converts it to `manual`, so
    synchronization can never take away a role an administrator has chosen to give.

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
        if granted_via == "manual" and getattr(existing, "granted_via", "manual") != "manual":
            existing.granted_via = "manual"
            await session.flush()
        return

    # Serialize with account anonymization (`core.privacy`): take a shared
    # lock on the person's global row and refuse an inactive/anonymized
    # account. Without this, a role granted (invitation accept / SSO
    # provisioning) concurrently with a data deletion could land on an
    # account that is being, or has just been, anonymized.
    if not await repository.lock_user_row(session, user_id, exclusive=False):
        raise PermissionDeniedError(
            "Cannot grant a role to an inactive or deleted account.",
            error_code="user.inactive",
            detail={"user_id": str(user_id)},
        )

    await repository.insert_user_role(
        session, user_id=user_id, organization_id=organization_id, role_id=role_id, granted_via=granted_via
    )
    logger.info(
        "role_assigned",
        user_id=str(user_id),
        organization_id=str(organization_id),
        role_id=str(role_id),
    )


async def is_organization_member(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> bool:
    """Whether `user_id` currently holds a role in `organization_id`."""
    return await repository.is_organization_member(session, user_id, organization_id)


async def lock_user_for_deletion(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """Take an exclusive lock on `user_id`'s global `users` row for the rest
    of the transaction. Returns whether the user exists.

    Used by data deletion so that (a) two organizations deleting the same
    multi-organization person are serialized and the second sees the first's
    committed result, and (b) `assign_role` (which takes the conflicting
    shared lock) cannot add a membership while the anonymization decision is
    being made.
    """
    return await repository.lock_user_row(session, user_id, exclusive=True) is not None


async def assert_organization_keeps_an_admin(
    session: AsyncSession,
    organization_id: uuid.UUID,
    removing_user_id: uuid.UUID,
    *,
    lock: bool,
) -> None:
    """Raise `ConflictError` if taking admin power away from
    `removing_user_id` would leave `organization_id` with no admin.

    Does nothing when `removing_user_id` is not currently an admin, or when
    at least one *other* admin would remain.

    `lock=False` is a plain read, for dry runs. `lock=True` is the form a
    mutating operation must use, *before* it mutates: it first takes a
    per-organization advisory lock and then re-reads (and row-locks) the
    admin set, so two concurrent operations are serialized and the second
    one sees the first one's committed result. Every present or future path
    that deletes or demotes an admin (data deletion today; role changes or
    removals later) must go through this function, or the guarantee does not
    hold -- the lock only protects callers that take it.
    """
    if lock:
        await repository.acquire_org_admin_lock(session, organization_id)
    admins = await repository.list_org_admin_user_ids(session, organization_id, lock=lock)
    if removing_user_id in admins and not (admins - {removing_user_id}):
        raise ConflictError(
            "This user is the organization's last admin. Grant admin access to "
            "another user before removing this one.",
            error_code="org.last_admin",
            detail={"organization_id": str(organization_id)},
        )


def authorize(
    actor: Identity, permission_code: str, project_id: uuid.UUID | None = None
) -> bool:
    """Pure authorization check: does `actor` hold `permission_code`?

    No database access -- operates on the permission set(s) already resolved
    onto the identity. With no `project_id`, checks the org-level set only
    (identical behavior to before this migration). With a `project_id`, checks
    that project's override if one exists, else falls back to the org-level
    set. This is the boolean form use `require_permission` when a denial should abort the
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


async def get_credential_lookup(session: AsyncSession, email: str) -> UserCredentialLookup | None:
    """Look up `email`'s local password credential, or `None` if no user
    with that email exists at all. See `UserCredentialLookup`'s own
    docstring for why this returns a narrow schema, not a `User` row.
    """
    row = await repository.get_by_email(session, email)
    if row is None:
        return None
    return UserCredentialLookup(
        user_id=row.id,
        password_hash=row.password_hash,
        is_active=row.is_active,
        email_verified_at=row.email_verified_at,
    )


def _credential_lookup(row) -> UserCredentialLookup:
    return UserCredentialLookup(
        user_id=row.id,
        password_hash=row.password_hash,
        is_active=row.is_active,
        email_verified_at=row.email_verified_at,
        email=row.email,
    )


async def get_credential_lookup_by_id(
    session: AsyncSession, user_id: uuid.UUID
) -> UserCredentialLookup | None:
    row = await repository.get_by_id(session, user_id)
    return _credential_lookup(row) if row is not None else None


async def get_credential_lookup_by_email_ci(
    session: AsyncSession, email: str
) -> UserCredentialLookup | None:
    """Case-insensitive variant, for flows that must find the account however the
    address was typed (password reset)."""
    row = await repository.get_by_email_ci(session, email)
    return _credential_lookup(row) if row is not None else None


async def replace_password(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    password_hash: str,
    sessions_valid_after: datetime,
) -> None:
    """Set a new password and the session cut-off together (reset / change).
    Callers revoke the refresh tokens in the same transaction."""
    if not await repository.replace_password_hash(
        session,
        user_id=user_id,
        password_hash=password_hash,
        sessions_valid_after=sessions_valid_after,
    ):
        raise NotFoundError("User not found.", error_code="user.not_found")


async def upgrade_password_hash(
    session: AsyncSession, *, user_id: uuid.UUID, old_hash: str, new_hash: str
) -> bool:
    """Re-hash the same password with the current algorithm (see
    `core.auth.passwords.needs_rehash`). Never changes who is signed in."""
    return await repository.upgrade_password_hash(
        session, user_id=user_id, old_hash=old_hash, new_hash=new_hash
    )


async def set_password(session: AsyncSession, *, user_id: uuid.UUID, password_hash: str) -> None:
    """Set `user_id`'s local password credential (already hashed by the
    caller -- `core/users` has no opinion on the hashing scheme, that's
    `core.auth.service`'s job). Only called from `core.auth.service.signup`,
    which needs `users` written without reaching into its table directly
    (see `repository.update_password_hash`'s own docstring).
    """
    await repository.update_password_hash(session, user_id=user_id, password_hash=password_hash)


async def resolve_organization_for_login(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    """Resolve which organization a password-authenticated `user_id` should
    log into -- `None` if they hold no role assignment anywhere (shouldn't
    happen for a `signup`-created account, but not assumed away). See
    `repository.get_first_organization_id`'s docstring for the current
    one-organization-per-password-account scope this reflects.
    """
    return await repository.get_first_organization_id(session, user_id)


async def list_organizations_for_login(session: AsyncSession, user_id: uuid.UUID) -> Sequence[uuid.UUID]:
    """Return every organization `user_id` holds a role in, so
    `core.auth.service.login_with_password` can tell "exactly one" apart
    from "more than one" and require explicit organization selection in the
    latter case, instead of silently picking one via
    `resolve_organization_for_login`/`get_first_organization_id` (both left
    unchanged -- this is a new, additive sibling, not a replacement).

    Empty means the user holds no role assignment anywhere -- the same case
    `resolve_organization_for_login` represents as `None`, just spelled as an
    empty sequence here since "zero of many" is the natural empty case for a
    listing rather than a special sentinel.
    """
    return await repository.list_organization_ids(session, user_id)


async def split_organizations_by_membership(
    session: AsyncSession, user_id: uuid.UUID
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """`(active, deactivated)` organization ids `user_id` holds a role in. Login, organization selection,
    switching and the organization list offer ONLY the active ones; the deactivated ones are returned so
    a caller can say "your access was deactivated" instead of a misleading "not a member"."""
    all_ids = list(await list_organizations_for_login(session, user_id))
    deactivated = await repository.list_deactivated_organization_ids(session, user_id)
    return (
        [org_id for org_id in all_ids if org_id not in deactivated],
        [org_id for org_id in all_ids if org_id in deactivated],
    )


_ADMIN_ROLE_NAME = "admin"


async def ensure_admin_role(session: AsyncSession) -> uuid.UUID:
    """Return the id of the platform's built-in `"admin"` role.

    The built-in role (and its grants) is provisioned by migration
    `e8c3a1f5b7d2` and is immutable to the application's database role, so on a
    migrated database this is a read. It still creates the role when it is
    absent (dev/test databases built from the models, or an owner-role script)
    and still *tries* to top up any grant that `ADMIN_PERMISSION_CODES` lists
    but the role lacks -- the old "self-heal on signup". Under row-level
    security that top-up is refused by design; the refusal is logged loudly and
    swallowed (the savepoint keeps the signup transaction alive) so a catalog
    that is ahead of the database degrades one permission, not every signup.
    The fix for that situation is a migration, never a request-time write.
    """
    role = await repository.get_role_by_name(session, _ADMIN_ROLE_NAME)
    if role is None:
        permissions = await repository.get_or_create_permissions(
            session, repository.ADMIN_PERMISSION_CODES
        )
        role = await repository.get_or_create_role_by_name(
            session, _ADMIN_ROLE_NAME, description=BUILTIN_ADMIN_ROLE_DESCRIPTION
        )
        await repository.grant_permissions_to_role(session, role_id=role.id, permissions=permissions)
        return role.id

    missing = await repository.get_missing_permission_codes(
        session, role.id, repository.ADMIN_PERMISSION_CODES
    )
    if missing:
        try:
            async with session.begin_nested():
                permissions = await repository.get_or_create_permissions(session, missing)
                await repository.grant_permissions_to_role(
                    session, role_id=role.id, permissions=permissions
                )
        except DBAPIError:
            logger.error("builtin_admin_role_missing_permissions", missing=sorted(missing))
    return role.id


def require_project_permission(
    actor: Identity, project_id: uuid.UUID, permission_code: str
) -> None:
    """Project-scoped `require_permission` -- for call sites that always have
    a specific project in hand and are never checking at the organization
    level only.

    Pure call-site clarity, not a new enforcement path: this is exactly
    `require_permission(actor, permission_code, project_id=project_id)`.
    Keeping both names lets a reader tell "this operation is scoped to one
    project, unconditionally" (this function) apart from "this operation
    optionally narrows to a project if one happens to be known"
    (`require_permission`'s own `project_id: ... | None` parameter) without
    reading the call site's surrounding context.
    """
    require_permission(actor, permission_code, project_id=project_id)
