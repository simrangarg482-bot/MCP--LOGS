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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.auth_models import RefreshToken
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
    """
    stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    result = await session.execute(stmt)
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
