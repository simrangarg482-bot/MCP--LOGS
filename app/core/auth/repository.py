"""Persistence for core/auth -- refresh tokens and external identity mappings.

Owned by: core/auth. Pure data access, same discipline as
core/tenancy/repository.py and core/users/repository.py: each function issues
one statement and returns ORM rows (or None / a count), with no business
rules and no ORM->Pydantic mapping -- that's service.py's job.

Ownership note on `ExternalIdentityMapping`: its ORM model is defined in
database/models/tenancy_models.py (grouped there physically alongside the
other tenancy tables), but per PROJECT_PLAN.md section 3.3/9.1 it is
core/auth's login flow that resolves and creates it, not core/tenancy's --
core/tenancy's own responsibilities (section 9.2) list organizations,
projects, connector configs, and SSO configs, never this table. This module
reads/writes it directly on that basis; tenancy_models.py's own module
docstring currently claims core/tenancy owns write access to everything in
that file, which is a pre-existing inconsistency worth reconciling in a
future documentation pass, not something reproduced further here.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.auth_models import EmailVerification, PasswordReset, RefreshToken
from app.database.models.tenancy_models import ExternalIdentityMapping

# --- Refresh tokens ----------------------------------------------------------


async def insert_refresh_token(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    family_id: uuid.UUID,
    token_hash: str,
    expires_at: datetime,
    oauth_client_id: str | None = None,
) -> RefreshToken:
    """Create one refresh token row and return it with server defaults populated.

    `family_id` is supplied by the caller (service.py), not generated here: on
    first login it's a freshly generated id; on rotation it's the same value
    carried forward from the token being rotated, which is what makes "revoke
    the whole family" meaningful (RefreshToken's model docstring).
    """
    row = RefreshToken(
        user_id=user_id,
        organization_id=organization_id,
        family_id=family_id,
        token_hash=token_hash,
        expires_at=expires_at,
        oauth_client_id=oauth_client_id,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_refresh_token_by_hash(
    session: AsyncSession, token_hash: str
) -> RefreshToken | None:
    """Fetch a refresh token by its hash, or None if no such token exists.

    Called with the hash of a client-presented token (hashed by service.py,
    never compared in plaintext) -- the caller is responsible for then
    checking `revoked_at`/`expires_at` before trusting the result.

    Milestone 10 RLS note: `refresh_tokens` is RLS-protected -- this only
    returns the row if `app.database.session.set_tenant_context` has
    already been set to match its `organization_id` on this session.
    `service.refresh`/`service.logout` resolve that first via
    `resolve_refresh_token_organization_id` below, since neither has any
    other org context available at the point this is called.
    """
    stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def resolve_refresh_token_organization_id(
    session: AsyncSession, token_hash: str
) -> uuid.UUID | None:
    """Resolve just the `organization_id` a refresh token belongs to,
    bypassing RLS via the narrow `resolve_refresh_token_organization` SQL
    function (see `d2e5f8a3c1b6_milestone_10_rls_bypass_functions.py`) --
    the same pattern as `app.ingestion.repository.
    resolve_connector_config_organization_id`.

    The one legitimate reason to call this: `core.auth.service.refresh`/
    `logout` start from a bare, client-presented token hash with no
    `Identity`/org context yet to `set_tenant_context` with -- this function
    exists solely to break that chicken-and-egg deadlock, returning nothing
    but the org id. The caller is expected to call `set_tenant_context` with
    the result, then call `get_refresh_token_by_hash` above for the actual
    row.

    Returns None if no such token hash exists (mirrors
    `get_refresh_token_by_hash`'s own `None`-for-missing convention).
    """
    result = await session.execute(
        text("SELECT resolve_refresh_token_organization(:token_hash)"),
        {"token_hash": token_hash},
    )
    return result.scalar_one_or_none()


async def revoke_refresh_token(
    session: AsyncSession, refresh_token_id: uuid.UUID, *, revoked_at: datetime
) -> RefreshToken | None:
    """Mark one refresh token revoked (rotation, or a single explicit logout),
    returning the updated row or None if it doesn't exist.

    Idempotent: revoking an already-revoked token just overwrites
    `revoked_at` with the same intent, rather than raising -- callers that
    need to distinguish "was this already revoked" (e.g. reuse detection)
    should check `revoked_at` on the row returned by `get_refresh_token_by_hash`
    before calling this, not rely on this function to tell them.
    """
    row = await session.get(RefreshToken, refresh_token_id)
    if row is None:
        return None
    row.revoked_at = revoked_at
    await session.flush()
    await session.refresh(row)
    return row


async def revoke_refresh_token_if_active(
    session: AsyncSession, refresh_token_id: uuid.UUID, *, revoked_at: datetime
) -> bool:
    """Atomically claim ONE refresh token for rotation: revoke it only if it is still active.

    A single conditional `UPDATE ... WHERE id = :id AND revoked_at IS NULL RETURNING id` -- never a
    read followed by a write. Under READ COMMITTED a concurrent request that already claimed the same
    token holds its row lock, so this statement waits for that transaction and then re-evaluates the
    predicate: it matches nothing and returns `False`. Of any number of simultaneous presentations of
    one token exactly one gets `True`; every other one learns it lost the race, which is reuse.

    Returns `True` iff this call revoked the row. Runs under the caller's tenant context (RLS applies
    to the UPDATE like to any other statement on `refresh_tokens`).
    """
    stmt = (
        update(RefreshToken)
        .where(RefreshToken.id == refresh_token_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=revoked_at)
        .returning(RefreshToken.id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def revoke_family(
    session: AsyncSession, family_id: uuid.UUID, *, revoked_at: datetime
) -> int:
    """Revoke every not-yet-revoked token in `family_id` in one statement.

    This is the reuse-detection response (RefreshToken's model docstring): a
    rotated-out token being presented again means the family is likely
    compromised, and every token descended from that login should stop
    working immediately, not just the one token that was replayed. Returns
    the number of rows actually revoked.
    """
    stmt = (
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=revoked_at)
    )
    result = await session.execute(stmt)
    return result.rowcount or 0


async def revoke_all_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    *,
    revoked_at: datetime,
) -> int:
    """Revoke every not-yet-revoked token for `user_id` within
    `organization_id` in one statement.

    Backs "log out everywhere" and admin-forced session termination
    (PROJECT_PLAN.md section 12.1). Scoped by organization as well as user,
    consistent with every other org-scoped query in this codebase -- a
    forced logout in one organization must not touch a session the same
    person holds in a different one. Returns the number of rows revoked.
    """
    stmt = (
        update(RefreshToken)
        .where(
            RefreshToken.user_id == user_id,
            RefreshToken.organization_id == organization_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=revoked_at)
    )
    result = await session.execute(stmt)
    return result.rowcount or 0


async def list_active_tokens_for_user(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> Sequence[RefreshToken]:
    """Return every not-yet-revoked token for `user_id` within
    `organization_id` -- e.g. for an admin/self-service "active sessions" view.
    """
    stmt = select(RefreshToken).where(
        RefreshToken.user_id == user_id,
        RefreshToken.organization_id == organization_id,
        RefreshToken.revoked_at.is_(None),
    )
    result = await session.execute(stmt)
    return result.scalars().all()


# --- External identity mappings ------------------------------------------------


async def get_external_identity_mapping(
    session: AsyncSession, organization_id: uuid.UUID, idp_subject: str
) -> ExternalIdentityMapping | None:
    """Resolve an IdP's subject claim to an EKIP user, within one organization.

    This is the lookup that completes step 6 of the SSO flow
    (PROJECT_PLAN.md section 3.3): "EKIP resolves (organization_id,
    idp_subject) against external_identity_mappings." Filtered by both
    columns, matching the model's own uniqueness constraint
    (`uq_external_identity_org_subject`) -- the same IdP subject in a
    different organization must resolve to a different mapping, or none.
    """
    stmt = select(ExternalIdentityMapping).where(
        ExternalIdentityMapping.organization_id == organization_id,
        ExternalIdentityMapping.idp_subject == idp_subject,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def insert_external_identity_mapping(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    idp_subject: str,
) -> ExternalIdentityMapping:
    """Create a new IdP-subject-to-user mapping (just-in-time provisioning).

    Called on a known-invited email's first successful login (PROJECT_PLAN.md
    section 3.3, step 6) -- deciding *whether* the email is actually invited
    or covered by a pre-approved domain/group rule is service.py's job; this
    function only performs the mechanical insert once that decision has
    already been made.
    """
    row = ExternalIdentityMapping(
        organization_id=organization_id, user_id=user_id, idp_subject=idp_subject
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


# --- Email verification challenges ---------------------------------------------
#
# `email_verifications` is global (not RLS-protected; see its model docstring),
# so none of these need a tenant context.


async def get_open_verification_for_update(
    session: AsyncSession, *, email: str, kind: str
) -> EmailVerification | None:
    """The single not-yet-consumed challenge for (email, kind), row-locked.

    `FOR UPDATE` serialises concurrent signup/resend requests for the same
    address so the cooldown/daily-cap bookkeeping in the service is not
    racy. The partial unique index guarantees at most one such row.
    """
    stmt = (
        select(EmailVerification)
        .where(
            EmailVerification.email == email,
            EmailVerification.kind == kind,
            EmailVerification.consumed_at.is_(None),
        )
        .with_for_update()
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def insert_open_verification_if_absent(
    session: AsyncSession, *, values: dict
) -> uuid.UUID | None:
    """INSERT ... ON CONFLICT DO NOTHING against the open-(email, kind)
    unique index. Returns the new id, or None if an open row already exists
    (a concurrent request won the race -- the caller re-selects it FOR UPDATE).
    """
    stmt = (
        pg_insert(EmailVerification)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=["email", "kind"], index_where=text("consumed_at IS NULL")
        )
        .returning(EmailVerification.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def consume_verification_by_hash(
    session: AsyncSession, *, token_hash: str, now: datetime
) -> EmailVerification | None:
    """THE single-use gate: atomically mark the matching, unconsumed,
    unexpired challenge consumed and return it.

    One statement, so two concurrent requests presenting the same token are
    serialised by Postgres' row lock; the loser re-evaluates the WHERE clause
    against the committed row, sees `consumed_at` set, and matches nothing.
    The caller runs this in the same transaction as the activation work, so
    if activation fails the consumption rolls back with it.
    """
    stmt = (
        update(EmailVerification)
        .where(
            EmailVerification.token_hash == token_hash,
            EmailVerification.consumed_at.is_(None),
            EmailVerification.expires_at > now,
        )
        .values(consumed_at=now)
        .returning(EmailVerification)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_verification_by_hash(
    session: AsyncSession, token_hash: str
) -> EmailVerification | None:
    """Read-only lookup used only to *explain* a failed consume (unknown /
    expired / already used) to the holder of the token."""
    stmt = select(EmailVerification).where(EmailVerification.token_hash == token_hash)
    return (await session.execute(stmt)).scalar_one_or_none()


async def delete_stale_verifications(
    session: AsyncSession, *, expired_before: datetime, consumed_before: datetime
) -> int:
    """Housekeeping: drop challenges that expired or were consumed before the
    given cut-offs. Never touches `users`/`organizations`."""
    from sqlalchemy import delete, or_

    stmt = delete(EmailVerification).where(
        or_(
            EmailVerification.expires_at < expired_before,
            EmailVerification.consumed_at < consumed_before,
        )
    )
    result = await session.execute(stmt)
    return result.rowcount or 0


async def resolve_invitation_organization_id(
    session: AsyncSession, invitation_id: uuid.UUID
) -> uuid.UUID | None:
    """Resolve only the owning organization of an invitation id through the
    narrow `resolve_invitation_organization` SECURITY DEFINER function (see
    `d5f2a8c1e9b4_email_verification.py`): the unauthenticated accept endpoint
    has no tenant context and `invitations` is FORCE RLS."""
    result = await session.execute(
        text("SELECT resolve_invitation_organization(:invitation_id)"),
        {"invitation_id": str(invitation_id)},
    )
    return result.scalar_one_or_none()


async def delete_external_identity_mapping(session: AsyncSession, mapping_id: uuid.UUID) -> None:
    """Remove one identity link (a stale one whose user is no longer a member)."""
    await session.execute(
        delete(ExternalIdentityMapping).where(ExternalIdentityMapping.id == mapping_id)
    )
    await session.flush()


# --- password reset links -----------------------------------------------------------


async def get_open_reset_for_update(
    session: AsyncSession, *, user_id: uuid.UUID
) -> PasswordReset | None:
    """The single not-yet-consumed reset link of `user_id`, row-locked so concurrent
    "forgot password" requests serialise on the cooldown / daily-cap bookkeeping."""
    stmt = (
        select(PasswordReset)
        .where(PasswordReset.user_id == user_id, PasswordReset.consumed_at.is_(None))
        .with_for_update()
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def insert_open_reset_if_absent(session: AsyncSession, *, values: dict) -> uuid.UUID | None:
    """INSERT ... ON CONFLICT DO NOTHING against the one-open-link-per-user index.
    Returns the new id, or None when a concurrent request created it first."""
    stmt = (
        pg_insert(PasswordReset)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["user_id"], index_where=text("consumed_at IS NULL"))
        .returning(PasswordReset.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def consume_reset_by_hash(
    session: AsyncSession, *, token_hash: str, now: datetime
) -> PasswordReset | None:
    """THE single-use gate: one statement marks the matching, unconsumed, unexpired
    link consumed and returns it. Two concurrent requests with the same token are
    serialised by the row lock; the loser re-evaluates the WHERE against the committed
    row and matches nothing. Runs in the same transaction as the password change, so a
    failure after it rolls the consumption back with everything else."""
    stmt = (
        update(PasswordReset)
        .where(
            PasswordReset.token_hash == token_hash,
            PasswordReset.consumed_at.is_(None),
            PasswordReset.expires_at > now,
        )
        .values(consumed_at=now)
        .returning(PasswordReset)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_reset_by_hash(session: AsyncSession, token_hash: str) -> PasswordReset | None:
    """Read-only lookup used only to EXPLAIN a failed consume to the token's holder."""
    stmt = select(PasswordReset).where(PasswordReset.token_hash == token_hash)
    return (await session.execute(stmt)).scalar_one_or_none()


async def consume_open_resets_for_user(
    session: AsyncSession, *, user_id: uuid.UUID, now: datetime
) -> int:
    """Kill every outstanding link of `user_id` (after a password change)."""
    result = await session.execute(
        update(PasswordReset)
        .where(PasswordReset.user_id == user_id, PasswordReset.consumed_at.is_(None))
        .values(consumed_at=now)
    )
    return result.rowcount or 0


async def delete_stale_resets(
    session: AsyncSession, *, expired_before: datetime, consumed_before: datetime
) -> int:
    """Housekeeping for expired / consumed links."""
    from sqlalchemy import delete, or_

    result = await session.execute(
        delete(PasswordReset).where(
            or_(
                PasswordReset.expires_at < expired_before,
                PasswordReset.consumed_at < consumed_before,
            )
        )
    )
    return result.rowcount or 0


async def revoke_all_for_organization(
    session: AsyncSession, organization_id: uuid.UUID, *, revoked_at: datetime
) -> int:
    """Revoke every not-yet-revoked refresh token of `organization_id` (every user, every MCP
    client) in one statement. Must run under that organization's tenant context -- the RLS
    policy then confines the UPDATE to its own rows regardless of the filter."""
    stmt = (
        update(RefreshToken)
        .where(
            RefreshToken.organization_id == organization_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=revoked_at)
    )
    result = await session.execute(stmt)
    return result.rowcount or 0


async def revoke_all_refresh_tokens_everywhere(
    session: AsyncSession, *, user_id: uuid.UUID, revoked_at: datetime
) -> int:
    """Revoke every still-valid refresh token of `user_id` in EVERY organization (and
    every MCP OAuth client) through the narrow `revoke_user_refresh_tokens` SECURITY
    DEFINER function -- `refresh_tokens` is FORCE row-level-security, so no tenant-scoped
    statement can reach other organizations' rows. Returns the number revoked."""
    result = await session.execute(
        text("SELECT revoke_user_refresh_tokens(:user_id, :revoked_at)"),
        {"user_id": user_id, "revoked_at": revoked_at},
    )
    return int(result.scalar_one() or 0)
