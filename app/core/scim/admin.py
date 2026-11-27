"""Tenant-administrator operations for SCIM provisioning (Settings -> Provisioning).

Every function needs `tenancy:manage` in the caller's OWN organization (`organization_id` in the path must equal
it). Nothing here is reachable by a SCIM token, and nothing here is a platform-operator control: a tenant
administrator manages their own organization's provisioning only.

The raw token value is returned by `create_token` / `rotate_token` and nowhere else; `ScimTokenInfo` carries only
the prefix, so a listing can never reveal a credential.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.service import record_audit_event
from app.core.exceptions import ConflictError, NotFoundError
from app.core.scim import repository, tokens
from app.core.scim import service as scim_service
from app.core.users import membership
from app.core.users import service as users_service
from app.core.users.permissions import ADMIN_PERMISSION
from app.database.models.tenancy_models import ScimToken
from app.shared.config.settings import get_settings
from app.shared.schemas import Identity


class ScimConfigUpdate(BaseModel):
    enabled: bool | None = None
    guard_min_absolute: int | None = Field(default=None, ge=1, le=10000)
    guard_percent: int | None = Field(default=None, ge=1, le=100)
    guard_window_minutes: int | None = Field(default=None, ge=1, le=1440)
    reactivation_settle_seconds: int | None = Field(default=None, ge=0, le=86400)


class ScimOverview(BaseModel):
    model_config = ConfigDict(frozen=True)

    #: The deployment-wide kill switch (`SCIM_ENABLED`). When false nothing below works, whatever the organization chose.
    platform_enabled: bool
    enabled: bool
    endpoint_url: str
    guard_min_absolute: int
    guard_percent: int
    guard_window_minutes: int
    reactivation_settle_seconds: int
    last_request_at: datetime | None
    last_success_at: datetime | None
    last_error_code: str | None
    last_error_at: datetime | None
    active_tokens: int
    pending_deactivations: int
    pending_identities: int


class ScimTokenInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    name: str
    prefix: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    status: str  # active | expired | revoked


class ScimTokenCreated(BaseModel):
    token: ScimTokenInfo
    #: Shown ONCE. Only its hash is stored; it cannot be retrieved again.
    raw_token: str


class ScimTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    ttl_days: int | None = Field(default=None, ge=1, le=3650)


class ScimTokenRotate(BaseModel):
    ttl_days: int | None = Field(default=None, ge=1, le=3650)


class CandidateMember(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str


class ScimIdentityInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    user_name: str
    external_id: str | None
    display_name: str | None
    active: bool
    linked_user_id: uuid.UUID | None
    linked_via: str | None
    linked_at: datetime | None
    last_seen_at: datetime
    #: For a pending identity: members whose email equals the userName. A HINT for the administrator only.
    candidates: tuple[CandidateMember, ...] = ()


class ScimIdentityPage(BaseModel):
    items: list[ScimIdentityInfo]
    total: int
    offset: int
    limit: int


class ScimIdentityLink(BaseModel):
    user_id: uuid.UUID


class PendingActionInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    user_id: uuid.UUID
    user_name: str
    reason: str
    status: str
    created_at: datetime
    decided_at: datetime | None


def _manage(actor: Identity, organization_id: uuid.UUID) -> None:
    users_service._ensure_same_organization(actor, organization_id)  # noqa: SLF001 - the shared tenant guard
    users_service.require_permission(actor, ADMIN_PERMISSION)


def _token_info(row: ScimToken, now: datetime) -> ScimTokenInfo:
    status = "revoked" if row.revoked_at is not None else ("expired" if row.expires_at <= now else "active")
    return ScimTokenInfo(
        id=row.id, name=row.name, prefix=row.token_prefix, created_at=row.created_at, expires_at=row.expires_at,
        last_used_at=row.last_used_at, revoked_at=row.revoked_at, status=status,
    )


# ---------------------------------------------------------------------------------------------- configuration


async def get_overview(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, *, base_url: str
) -> ScimOverview:
    _manage(actor, organization_id)
    config = await repository.get_or_create_config(session, organization_id)
    now = datetime.now(timezone.utc)
    pending = await repository.list_pending(session, organization_id)
    _identities, pending_identities = await repository.list_identities(
        session, organization_id, offset=0, limit=1, status="pending"
    )
    return ScimOverview(
        platform_enabled=get_settings().scim_enabled,
        enabled=config.enabled,
        endpoint_url=base_url.rstrip("/") + "/scim/v2",
        guard_min_absolute=config.guard_min_absolute,
        guard_percent=config.guard_percent,
        guard_window_minutes=config.guard_window_minutes,
        reactivation_settle_seconds=config.reactivation_settle_seconds,
        last_request_at=config.last_request_at,
        last_success_at=config.last_success_at,
        last_error_code=config.last_error_code,
        last_error_at=config.last_error_at,
        active_tokens=await repository.count_active_tokens(session, organization_id, now=now),
        pending_deactivations=len(pending),
        pending_identities=pending_identities,
    )


async def update_config(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, data: ScimConfigUpdate, *, base_url: str
) -> ScimOverview:
    _manage(actor, organization_id)
    config = await repository.get_or_create_config(session, organization_id)
    changes: dict = {}
    for field in ("enabled", "guard_min_absolute", "guard_percent", "guard_window_minutes", "reactivation_settle_seconds"):
        value = getattr(data, field)
        if value is not None and value != getattr(config, field):
            changes[field] = value
            setattr(config, field, value)
    if changes:
        config.updated_at = datetime.now(timezone.utc)
        await session.flush()
        await record_audit_event(
            session, actor, action="scim.config_updated", resource_type="scim_configuration", resource_id=organization_id,
            metadata={"changes": changes},
        )
    return await get_overview(session, actor, organization_id, base_url=base_url)


# ---------------------------------------------------------------------------------------------- tokens


async def list_tokens(session: AsyncSession, actor: Identity, organization_id: uuid.UUID) -> list[ScimTokenInfo]:
    _manage(actor, organization_id)
    now = datetime.now(timezone.utc)
    return [_token_info(row, now) for row in await repository.list_tokens(session, organization_id)]


async def create_token(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, data: ScimTokenCreate
) -> ScimTokenCreated:
    _manage(actor, organization_id)
    row, raw = await tokens.create_token(session, actor, name=data.name, ttl_days=data.ttl_days)
    return ScimTokenCreated(token=_token_info(row, datetime.now(timezone.utc)), raw_token=raw)


async def rotate_token(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, token_id: uuid.UUID, data: ScimTokenRotate
) -> ScimTokenCreated:
    _manage(actor, organization_id)
    row, raw = await tokens.rotate_token(session, actor, token_id, ttl_days=data.ttl_days)
    return ScimTokenCreated(token=_token_info(row, datetime.now(timezone.utc)), raw_token=raw)


async def revoke_token(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, token_id: uuid.UUID
) -> ScimTokenInfo:
    _manage(actor, organization_id)
    row = await tokens.revoke_token(session, actor, token_id)
    return _token_info(row, datetime.now(timezone.utc))


# ---------------------------------------------------------------------------------------------- identities


async def list_identities(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, *, status: str | None, offset: int, limit: int
) -> ScimIdentityPage:
    _manage(actor, organization_id)
    rows, total = await repository.list_identities(session, organization_id, offset=offset, limit=limit, status=status)
    items = []
    for row in rows:
        candidates: tuple[CandidateMember, ...] = ()
        if row.user_id is None:
            candidates = tuple(
                CandidateMember(user_id=u, email=e, display_name=d)
                for u, e, d in await repository.find_candidate_members(session, organization_id, row.user_name_key)
            )
        items.append(
            ScimIdentityInfo(
                id=row.id, user_name=row.user_name, external_id=row.external_id, display_name=row.display_name,
                active=row.active, linked_user_id=row.user_id, linked_via=row.linked_via, linked_at=row.linked_at,
                last_seen_at=row.last_seen_at, candidates=candidates,
            )
        )
    return ScimIdentityPage(items=items, total=total, offset=offset, limit=limit)


async def link_identity(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, identity_id: uuid.UUID, data: ScimIdentityLink
) -> ScimIdentityInfo:
    """An administrator vouches that this SCIM identity IS this member. The only manual linking path."""
    _manage(actor, organization_id)
    identity = await repository.get_identity(session, organization_id, identity_id, lock=True)
    if identity is None:
        raise NotFoundError("SCIM identity not found.", error_code="scim.identity_not_found")
    config = await repository.get_config(session, organization_id)
    await scim_service.link_identity(session, actor, config, identity, data.user_id, via="admin")
    return ScimIdentityInfo(
        id=identity.id, user_name=identity.user_name, external_id=identity.external_id,
        display_name=identity.display_name, active=identity.active, linked_user_id=identity.user_id,
        linked_via=identity.linked_via, linked_at=identity.linked_at, last_seen_at=identity.last_seen_at,
    )


# ---------------------------------------------------------------------------------------------- held deactivations


async def list_pending(session: AsyncSession, actor: Identity, organization_id: uuid.UUID) -> list[PendingActionInfo]:
    _manage(actor, organization_id)
    return [
        PendingActionInfo(
            id=p.id, user_id=p.user_id, user_name=i.user_name, reason=p.reason, status=p.status,
            created_at=p.created_at, decided_at=p.decided_at,
        )
        for p, i in await repository.list_pending(session, organization_id)
    ]


async def decide_pending(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, pending_id: uuid.UUID, *, approve: bool
) -> PendingActionInfo:
    """Approve (apply the held deactivation now, recorded as approved by this administrator) or reject it."""
    _manage(actor, organization_id)
    pending = await repository.get_pending(session, organization_id, pending_id, lock=True)
    if pending is None:
        raise NotFoundError("Pending action not found.", error_code="scim.pending_not_found")
    if pending.status != "pending":
        raise ConflictError("This action has already been decided.", error_code="scim.pending_decided")
    identity = await repository.get_identity(session, organization_id, pending.scim_identity_id)
    now = datetime.now(timezone.utc)
    if approve:
        try:
            await membership.deactivate_member_as_system(
                session, actor, organization_id, pending.user_id,
                reason=f"{pending.reason} (approved by an administrator)", source="scim",
                extra_metadata={"approved_by": str(actor.user_id), "pending_action_id": str(pending.id)},
            )
        except ConflictError as exc:
            if exc.error_code != "member.already_deactivated":
                raise
    pending.status = "approved" if approve else "rejected"
    pending.decided_by = actor.user_id
    pending.decided_at = now
    await session.flush()
    await record_audit_event(
        session, actor, action="scim.deactivation_approved" if approve else "scim.deactivation_rejected",
        resource_type="user", resource_id=pending.user_id,
        metadata={"pending_action_id": str(pending.id), "reason": pending.reason},
    )
    return PendingActionInfo(
        id=pending.id, user_id=pending.user_id, user_name=identity.user_name if identity else "", reason=pending.reason,
        status=pending.status, created_at=pending.created_at, decided_at=pending.decided_at,
    )
