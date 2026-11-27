"""Organization-scoped member offboarding: deactivate, reactivate and read a member's access state.

Owned by: core/users. The business rules live here; raw SQL lives in `repository.py`; HTTP lives in
`app/api/routers/members.py`.

WHAT "DEACTIVATED" MEANS
    A member whose access to ONE organization is switched off. It is NOT `users.is_active` (that flag is
    global and is never touched here: the same person may belong to other organizations, with other
    identity providers or a password), and it deletes nothing: connectors, incidents, knowledge and every
    other organization-owned record stay exactly as they are. Their role assignments stay too, so an
    administrator can reactivate them without re-granting anything.

HOW ACCESS IS CUT IMMEDIATELY (all in one transaction)
    1. The state row is written; the DATABASE bumps `member_epoch` (trigger).
    2. Every refresh-token family of that person in that organization is revoked (MCP OAuth refresh tokens
       are the same rows, so they go with them).
    3. `core.users.service.resolve_identity` -- the one check every REST request, MCP tool call and MCP
       authorization passes through -- refuses a deactivated member on the very next request, and
       `core.auth.service._issue_session` refuses to mint any new session for them.
    4. Access tokens carry the epoch they were issued under (`me`); reactivation bumps it again, so a token
       issued before the deactivation can never revive. The member must sign in again.

SAFEGUARDS (same patterns as `core.privacy` and `core.users.role_service`)
    * the actor needs `tenancy:manage` in THIS organization (never a caller-supplied organization);
    * the target must be a current member (a stranger, another organization's user and an unknown id all get
      the identical 404 -- the endpoint cannot be used to probe or damage other tenants);
    * an administrator cannot deactivate themselves;
    * the organization's last administrator cannot be deactivated (per-organization advisory lock taken
      first, admin set re-read under it -- deactivated admins do not count);
    * a reason is mandatory and is stored in the state row and the audit event.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.service import record_audit_event
from app.core.auth import repository as auth_repository
from app.core.exceptions import ConflictError, NotFoundError
from app.core.users import repository
from app.core.users import service as users_service
from app.core.users.permissions import ADMIN_PERMISSION
from app.core.users.schemas import MemberStatus
from app.database.session import set_tenant_context
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity

logger = get_logger(__name__)

# One refusal everywhere (defined in `service` because `resolve_identity` lives there and this module imports it).
membership_deactivated_error = users_service.membership_deactivated_error


def _require_manage(actor: Identity, organization_id: uuid.UUID) -> None:
    users_service._ensure_same_organization(actor, organization_id)  # noqa: SLF001 - the shared tenant guard
    users_service.require_permission(actor, ADMIN_PERMISSION)


async def _require_member(session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID) -> None:
    # 404 for a stranger and for a member of another organization alike.
    if not await repository.is_organization_member(session, user_id, organization_id):
        raise NotFoundError("Member not found.", error_code="member.not_found", detail={"user_id": str(user_id)})


def _to_status(organization_id: uuid.UUID, user_id: uuid.UUID, row) -> MemberStatus:
    if row is None:
        return MemberStatus(user_id=user_id, organization_id=organization_id, status="active")
    return MemberStatus(
        user_id=user_id,
        organization_id=organization_id,
        status=row.status,
        source=row.source,
        reason=row.reason,
        deactivated_at=row.deactivated_at,
        updated_at=row.updated_at,
        member_epoch=row.member_epoch,
    )


async def get_member_status(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, user_id: uuid.UUID
) -> MemberStatus:
    """A member's current state (administrators only). `active` when no state row exists."""
    _require_manage(actor, organization_id)
    await _require_member(session, organization_id, user_id)
    return _to_status(organization_id, user_id, await repository.get_member_state(session, organization_id, user_id))


async def assert_member_active(session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID) -> int:
    """Raise `auth.membership_deactivated` if `user_id` is deactivated in `organization_id`; otherwise
    return their member epoch (0 when they have no state row). Sets the tenant context itself: this is
    called from authentication paths that may run before any is set."""
    await set_tenant_context(session, organization_id)
    deactivated, epoch = await repository.get_member_access_state(session, organization_id, user_id)
    if deactivated:
        raise membership_deactivated_error()
    return epoch


async def _deactivate_core(
    session: AsyncSession,
    audit_actor: Identity,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    reason: str,
    source: str,
    extra_metadata: dict | None = None,
) -> MemberStatus:
    """The shared deactivation: member check, locks, last-admin guard, state, revocation, audit. Callers
    have already authorized the request (an administrator, or the SCIM service acting for an organization's
    own token)."""
    await _require_member(session, organization_id, user_id)

    # Lock order matters (deadlock otherwise): the per-organization admin lock first, then row locks --
    # the same order everywhere (core.privacy, core.users.role_service).
    await repository.acquire_org_admin_lock(session, organization_id)
    current = await repository.lock_member_state(session, organization_id, user_id)
    if current is not None and current.status == "deactivated":
        raise ConflictError(
            "This member is already deactivated.",
            error_code="member.already_deactivated",
            detail={"user_id": str(user_id)},
        )
    await users_service.assert_organization_keeps_an_admin(session, organization_id, user_id, lock=True)

    row = await repository.set_member_state(
        session, organization_id, user_id, status="deactivated", source=source, reason=reason
    )
    revoked = await auth_repository.revoke_all_for_user(
        session, user_id, organization_id, revoked_at=datetime.now(timezone.utc)
    )
    await record_audit_event(
        session,
        audit_actor,
        action="member.deactivate",
        resource_type="user",
        resource_id=user_id,
        metadata={
            "source": source,
            "reason": reason,
            "member_epoch": row.member_epoch,
            "sessions_revoked": revoked,
            **(extra_metadata or {}),
        },
    )
    logger.info(
        "member_deactivated",
        organization_id=str(organization_id),
        user_id=str(user_id),
        source=source,
        refresh_tokens_revoked=revoked,
    )
    return _to_status(organization_id, user_id, row)


async def _reactivate_core(
    session: AsyncSession,
    audit_actor: Identity,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    reason: str,
    source: str,
    extra_metadata: dict | None = None,
) -> MemberStatus:
    await _require_member(session, organization_id, user_id)

    await repository.acquire_org_admin_lock(session, organization_id)
    current = await repository.lock_member_state(session, organization_id, user_id)
    if current is None or current.status != "deactivated":
        raise ConflictError(
            "This member is not deactivated.", error_code="member.not_deactivated", detail={"user_id": str(user_id)}
        )

    row = await repository.set_member_state(
        session, organization_id, user_id, status="active", source=source, reason=reason
    )
    # Defence in depth: nothing can have been issued while deactivated, but "reactivation keeps every old
    # session invalid" must not depend on that, so any survivor is revoked here too.
    revoked = await auth_repository.revoke_all_for_user(
        session, user_id, organization_id, revoked_at=datetime.now(timezone.utc)
    )
    await record_audit_event(
        session,
        audit_actor,
        action="member.reactivate",
        resource_type="user",
        resource_id=user_id,
        metadata={
            "source": source,
            "reason": reason,
            "member_epoch": row.member_epoch,
            "sessions_revoked": revoked,
            **(extra_metadata or {}),
        },
    )
    logger.info("member_reactivated", organization_id=str(organization_id), user_id=str(user_id), source=source)
    return _to_status(organization_id, user_id, row)


async def deactivate_member(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    reason: str,
    source: str = "admin",
) -> MemberStatus:
    """Cut one member's access to this organization, now. Deactivating someone who is already deactivated is
    a 409, never silently accepted."""
    _require_manage(actor, organization_id)
    if actor.user_id is not None and actor.user_id == user_id:
        raise ConflictError("You cannot deactivate yourself.", error_code="member.cannot_deactivate_self")
    return await _deactivate_core(session, actor, organization_id, user_id, reason=reason, source=source)


async def reactivate_member(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    reason: str,
    source: str = "admin",
) -> MemberStatus:
    """Restore a deactivated member's access. Issues NO session: the member signs in again, and every
    access token and refresh token from before the deactivation stays dead (the epoch moves again, and
    the refresh tokens were revoked)."""
    _require_manage(actor, organization_id)
    return await _reactivate_core(session, actor, organization_id, user_id, reason=reason, source=source)


async def deactivate_member_as_system(
    session: AsyncSession,
    audit_actor: Identity,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    reason: str,
    source: str,
    extra_metadata: dict | None = None,
) -> MemberStatus:
    """Deactivation requested by an organization's own provisioning channel (SCIM) or approved by an
    administrator on its behalf. `audit_actor` names who is recorded; the same last-administrator guard,
    session revocation and audit apply. There is no permission check here: callers are the SCIM service
    (authenticated by an organization-scoped token) and the approval endpoint (which checks the permission)."""
    return await _deactivate_core(
        session, audit_actor, organization_id, user_id, reason=reason, source=source, extra_metadata=extra_metadata
    )


async def reactivate_member_as_system(
    session: AsyncSession,
    audit_actor: Identity,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    reason: str,
    source: str,
    extra_metadata: dict | None = None,
) -> MemberStatus:
    return await _reactivate_core(
        session, audit_actor, organization_id, user_id, reason=reason, source=source, extra_metadata=extra_metadata
    )
