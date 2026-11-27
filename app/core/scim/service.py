"""SCIM Users: lookup, creation of a (pending) SCIM identity, update, deactivation and deletion.

WHAT SCIM CAN AND CANNOT DO HERE
    * It records what the identity provider says about a user (`scim_identities`). That record is NOT an
      EKIP account and grants nothing: SCIM never creates a `users` row, never assigns a role (until Phase-4
      group mappings, configured by an administrator) and never changes `users.is_active`.
    * A SCIM identity is LINKED to an existing member only by proof or by an administrator:
        - proof: its `externalId` equals the subject EKIP already verified through OIDC for that member
          (`external_identity_mappings.idp_subject`) -- the same identity provider vouched for both;
        - an administrator links it by hand in Settings -> Provisioning.
      A matching email is shown to the administrator as a HINT. It never links anything by itself.
    * `active=false` (PATCH or PUT) and DELETE deactivate the linked member's access to THIS organization
      through `core.users.membership` (refresh tokens revoked, member epoch bumped, audit), behind the
      mass-deactivation guard and the last-administrator guard.
    * `active=true` reactivates only a member that SCIM itself deactivated (never one an administrator
      deactivated), and only after a settle period, so a late, out-of-order `true` cannot undo a newer `false`.

EVERY operation is idempotent: repeating a deactivation, a deletion or a re-enable is a no-op that answers
the same as the first call.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.service import record_audit_event
from app.core.auth import repository as auth_repository
from app.core.exceptions import ConflictError
from app.core.scim import groups, repository, schemas
from app.core.scim.errors import ScimError
from app.core.scim.schemas import UserFields
from app.core.users import membership
from app.core.users import repository as users_repository
from app.database.models.tenancy_models import ScimConfiguration, ScimIdentity
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity

logger = get_logger(__name__)


@dataclass
class ScimContext:
    """Who is calling: one organization's token, with that organization's configuration."""

    organization_id: uuid.UUID
    token_id: uuid.UUID
    token_prefix: str
    config: ScimConfiguration
    base_url: str

    @property
    def audit_actor(self) -> Identity:
        return Identity.for_agent(f"scim:{self.token_prefix}", self.organization_id)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------------------------- reading


async def _resources(session: AsyncSession, ctx: ScimContext, identities: list[ScimIdentity]) -> list[dict]:
    linked = [i.user_id for i in identities if i.user_id is not None]
    deactivated = await repository.deactivated_user_ids(session, ctx.organization_id, linked)
    out = []
    for identity in identities:
        # EKIP is the source of truth for a linked member: a member an administrator deactivated is reported
        # inactive whatever the identity provider last said.
        active = identity.active if identity.user_id is None else (identity.user_id not in deactivated)
        out.append(schemas.user_resource(identity, ctx.base_url, effective_active=active))
    return out


async def _require_identity(session: AsyncSession, ctx: ScimContext, identity_id: str, *, lock: bool = False) -> ScimIdentity:
    try:
        key = uuid.UUID(identity_id)
    except ValueError:
        raise ScimError(404, "User not found.", code="scim.not_found") from None
    identity = await repository.get_identity(session, ctx.organization_id, key, lock=lock)
    if identity is None:
        raise ScimError(404, "User not found.", code="scim.not_found")
    return identity


async def get_user(session: AsyncSession, ctx: ScimContext, identity_id: str) -> dict:
    identity = await _require_identity(session, ctx, identity_id)
    return (await _resources(session, ctx, [identity]))[0]


async def list_users(
    session: AsyncSession, ctx: ScimContext, *, filter_text: str | None, start_index: int | None, count: int | None
) -> dict:
    parsed = schemas.parse_filter(filter_text)
    start, size = schemas.clamp_paging(start_index, count)
    rows, total = await repository.list_identities(
        session,
        ctx.organization_id,
        offset=start - 1,
        limit=size,
        user_name_key=schemas.user_name_key(parsed[1]) if parsed and parsed[0] == "userName" else None,
        external_id=parsed[1] if parsed and parsed[0] == "externalId" else None,
    )
    return schemas.list_response(await _resources(session, ctx, rows) if size else [], total, start)


# ---------------------------------------------------------------------------------------------- linking


async def link_identity(
    session: AsyncSession,
    audit_actor: Identity,
    config: ScimConfiguration | None,
    identity: ScimIdentity,
    user_id: uuid.UUID,
    *,
    via: str,
) -> None:
    """Bind a pending SCIM identity to an existing member. The caller has established the proof (`sso_subject`)
    or the administrator's decision (`admin`). Both sides must be free, and the user must currently be a member
    of the organization. If the identity provider already said this person is inactive, they are offboarded now."""
    organization_id = identity.organization_id
    if identity.user_id is not None:
        raise ConflictError("This SCIM identity is already linked.", error_code="scim.already_linked")
    if not await users_repository.is_organization_member(session, user_id, organization_id):
        raise ConflictError("That person is not a member of this organization.", error_code="scim.not_a_member")
    if await repository.find_identity_by_user(session, organization_id, user_id) is not None:
        raise ConflictError("That member is already linked to another SCIM identity.", error_code="scim.user_already_linked")
    identity.user_id = user_id
    identity.linked_via = via
    identity.linked_at = _now()
    identity.updated_at = _now()
    await session.flush()
    await record_audit_event(
        session, audit_actor, action="scim.identity_linked", resource_type="user", resource_id=user_id,
        metadata={"via": via, "scim_identity_id": str(identity.id), "user_name": identity.user_name},
    )
    if not identity.active:
        await offboard(session, audit_actor, config, identity, reason="Inactive in the identity provider when linked")
    else:
        # Group memberships the provider pushed before this person was linked now take effect.
        await groups.reconcile_user(session, audit_actor, organization_id, user_id, config)


async def _try_proof_link(session: AsyncSession, ctx: ScimContext, identity: ScimIdentity) -> None:
    """Link by PROOF only: the externalId is a subject this organization already verified via OIDC for a
    member. No proof, no link (and never email)."""
    if identity.user_id is not None or not identity.external_id:
        return
    mapping = await auth_repository.get_external_identity_mapping(session, ctx.organization_id, identity.external_id)
    if mapping is None:
        return
    try:
        await link_identity(session, ctx.audit_actor, ctx.config, identity, mapping.user_id, via="sso_subject")
    except ConflictError:
        return  # the member left, or is already linked elsewhere: stay pending for an administrator


async def link_pending_identity_for_sso_login(
    session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID, idp_subject: str
) -> None:
    """Called after a SUCCESSFUL, verified SSO login: if the identity provider has told us about a SCIM
    identity whose externalId is this very subject, that is proof both describe the same person. Best effort:
    it must never break a login, so it runs in a savepoint and logs instead of raising."""
    try:
        async with session.begin_nested():
            identity = await repository.find_identity_by_external_id(session, organization_id, idp_subject)
            if identity is None or identity.user_id is not None:
                return
            config = await repository.get_config(session, organization_id)
            await link_identity(
                session, Identity.for_agent("scim:sso-login", organization_id), config, identity, user_id, via="sso_subject"
            )
    except ConflictError:
        return
    except ScimError:
        return
    except Exception:  # noqa: BLE001 - a login must never fail because of this
        logger.exception("scim_sso_link_failed", organization_id=str(organization_id))


# ---------------------------------------------------------------------------------------------- offboarding


# The mass-deactivation guard lives with group synchronization (which also deactivates); one definition.
_guard_trips = groups.guard_trips


async def offboard(
    session: AsyncSession,
    audit_actor: Identity,
    config: ScimConfiguration | None,
    identity: ScimIdentity,
    *,
    reason: str,
) -> str:
    """Deactivate the linked member's access to this organization. Returns `already` / `not_member` /
    `deactivated`; raises `ScimError(409)` when the request is HELD or REFUSED (the caller answers the IdP,
    which retries on its next cycle). Idempotent."""
    organization_id = identity.organization_id
    user_id = identity.user_id
    if user_id is None:
        return "not_linked"
    state = await users_repository.get_member_state(session, organization_id, user_id)
    if state is not None and state.status == "deactivated":
        return "already"
    if not await users_repository.is_organization_member(session, user_id, organization_id):
        return "not_member"

    if await _guard_trips(session, config, organization_id):
        pending = await repository.get_pending_for_identity(session, organization_id, identity.id)
        if pending is None:
            await repository.insert_pending(
                session, organization_id=organization_id, scim_identity_id=identity.id, user_id=user_id, reason=reason
            )
        await record_audit_event(
            session, audit_actor, action="scim.deactivation_held", resource_type="user", resource_id=user_id,
            metadata={"reason": reason, "scim_identity_id": str(identity.id), "guard": "mass_deactivation"},
        )
        raise ScimError(
            409,
            "Deactivation is held for administrator approval (too many deactivations in a short time).",
            "mutability",
            code="scim.deactivation_held",
        )
    try:
        await membership.deactivate_member_as_system(
            session, audit_actor, organization_id, user_id, reason=reason, source="scim",
            extra_metadata={"scim_identity_id": str(identity.id)},
        )
    except ConflictError as exc:
        if exc.error_code == "member.already_deactivated":
            return "already"
        if exc.error_code == "org.last_admin":
            await record_audit_event(
                session, audit_actor, action="scim.deactivation_refused", resource_type="user", resource_id=user_id,
                metadata={"reason": "last_admin", "scim_identity_id": str(identity.id)},
            )
            raise ScimError(
                409,
                "This person is the organization's last administrator and cannot be deactivated.",
                "mutability",
                code="scim.last_admin",
            ) from exc
        raise
    return "deactivated"


async def _reenable(session: AsyncSession, ctx: ScimContext, identity: ScimIdentity) -> None:
    """`active=true`: reactivate only a member SCIM itself deactivated, and only once the settle period has
    passed. Anything else is a quiet no-op (audited) -- never an override of an administrator's decision."""
    # an administrator-pending hold is simply withdrawn
    pending = await repository.get_pending_for_identity(session, ctx.organization_id, identity.id)
    if pending is not None:
        pending.status = "rejected"
        pending.decided_at = _now()
        await session.flush()
    user_id = identity.user_id
    if user_id is None:
        return
    state = await users_repository.get_member_state(session, ctx.organization_id, user_id)
    if state is None or state.status != "deactivated":
        return
    if state.source != "scim":
        await record_audit_event(
            session, ctx.audit_actor, action="scim.reactivation_ignored", resource_type="user", resource_id=user_id,
            metadata={"reason": "deactivated_by_administrator", "scim_identity_id": str(identity.id)},
        )
        return
    settle = timedelta(seconds=ctx.config.reactivation_settle_seconds)
    if state.deactivated_at is not None and _now() - state.deactivated_at < settle:
        await record_audit_event(
            session, ctx.audit_actor, action="scim.reactivation_deferred", resource_type="user", resource_id=user_id,
            metadata={"reason": "settling", "scim_identity_id": str(identity.id)},
        )
        raise ScimError(
            409,
            "This user was deactivated moments ago; the re-enable is deferred. Retry on the next sync.",
            "mutability",
            code="scim.reactivation_deferred",
        )
    await membership.reactivate_member_as_system(
        session, ctx.audit_actor, ctx.organization_id, user_id,
        reason="Re-enabled in the identity provider", source="scim",
        extra_metadata={"scim_identity_id": str(identity.id)},
    )
    await groups.reconcile_user(session, ctx.audit_actor, ctx.organization_id, user_id, ctx.config)


# ---------------------------------------------------------------------------------------------- writing


async def _apply_fields(
    session: AsyncSession, ctx: ScimContext, identity: ScimIdentity, fields: UserFields
) -> None:
    now = _now()
    org = ctx.organization_id
    if "userName" in fields.provided and fields.user_name:
        key = schemas.user_name_key(fields.user_name)
        if key != identity.user_name_key:
            clash = await repository.find_identity_by_user_name(session, org, key)
            if clash is not None and clash.id != identity.id:
                raise ScimError(409, "That userName is already in use.", "uniqueness", code="scim.uniqueness")
            identity.user_name, identity.user_name_key = fields.user_name, key
    if "externalId" in fields.provided and fields.external_id != identity.external_id:
        if fields.external_id is not None:
            clash = await repository.find_identity_by_external_id(session, org, fields.external_id)
            if clash is not None and clash.id != identity.id:
                raise ScimError(409, "That externalId is already in use.", "uniqueness", code="scim.uniqueness")
        identity.external_id = fields.external_id
    if "displayName" in fields.provided:
        identity.display_name = fields.display_name
    identity.last_seen_at = now
    identity.updated_at = now
    await session.flush()
    await _try_proof_link(session, ctx, identity)


async def _apply_active(session: AsyncSession, ctx: ScimContext, identity: ScimIdentity, active: bool) -> None:
    previous = identity.active
    identity.active = active
    identity.updated_at = _now()
    await session.flush()
    if active:
        await _reenable(session, ctx, identity)
    else:
        await offboard(
            session, ctx.audit_actor, ctx.config, identity,
            reason="Deactivated in the identity provider" if previous else "Inactive in the identity provider",
        )


async def create_user(session: AsyncSession, ctx: ScimContext, payload: object) -> dict:
    fields = schemas.parse_user_payload(payload, require_user_name=True)
    assert fields.user_name is not None
    key = schemas.user_name_key(fields.user_name)
    if await repository.find_identity_by_user_name(session, ctx.organization_id, key) is not None:
        raise ScimError(409, "A user with that userName already exists.", "uniqueness", code="scim.uniqueness")
    if fields.external_id and await repository.find_identity_by_external_id(session, ctx.organization_id, fields.external_id):
        raise ScimError(409, "A user with that externalId already exists.", "uniqueness", code="scim.uniqueness")
    identity = await repository.insert_identity(
        session,
        organization_id=ctx.organization_id,
        user_name=fields.user_name,
        user_name_key=key,
        external_id=fields.external_id,
        display_name=fields.display_name,
        active=True if fields.active is None else fields.active,
    )
    await record_audit_event(
        session, ctx.audit_actor, action="scim.user_created", resource_type="scim_identity", resource_id=identity.id,
        metadata={"user_name": identity.user_name, "external_id": identity.external_id},
    )
    await _try_proof_link(session, ctx, identity)
    if identity.user_id is not None and not identity.active:
        pass  # link_identity already offboarded an inactive identity
    return (await _resources(session, ctx, [identity]))[0]


async def replace_user(session: AsyncSession, ctx: ScimContext, identity_id: str, payload: object) -> dict:
    identity = await _require_identity(session, ctx, identity_id, lock=True)
    fields = schemas.parse_user_payload(payload, require_user_name=False)
    await _apply_fields(session, ctx, identity, fields)
    if fields.active is not None:
        await _apply_active(session, ctx, identity, fields.active)
    return (await _resources(session, ctx, [identity]))[0]


async def patch_user(session: AsyncSession, ctx: ScimContext, identity_id: str, payload: object) -> dict:
    identity = await _require_identity(session, ctx, identity_id, lock=True)
    fields = schemas.parse_patch(payload)
    await _apply_fields(session, ctx, identity, fields)
    if fields.active is not None:
        await _apply_active(session, ctx, identity, fields.active)
    return (await _resources(session, ctx, [identity]))[0]


async def delete_user(session: AsyncSession, ctx: ScimContext, identity_id: str) -> None:
    """DELETE means "removed from the identity provider": the linked member's access to THIS organization is
    deactivated (nothing is deleted from EKIP) and the SCIM identity is tombstoned. A repeat is a 404, as the
    RFC says -- and identity providers treat that as success."""
    identity = await _require_identity(session, ctx, identity_id, lock=True)
    await offboard(session, ctx.audit_actor, ctx.config, identity, reason="Deleted in the identity provider")
    identity.active = False
    identity.deleted_at = _now()
    identity.updated_at = _now()
    await session.flush()
    await groups.forget_identity_memberships(session, ctx.organization_id, ctx.audit_actor, identity.id, identity.user_id)
    await record_audit_event(
        session, ctx.audit_actor, action="scim.user_deleted", resource_type="scim_identity", resource_id=identity.id,
        metadata={"user_name": identity.user_name, "linked": identity.user_id is not None},
    )
