"""SCIM bearer tokens: generation, hashing, authentication, rotation, revocation.

* The raw token is `scim_` + 36 random bytes (urlsafe); it is returned ONCE, at creation. Only its SHA-256 hash
  is stored (a token is high-entropy and random, so a fast hash is the right tool, exactly as for refresh
  tokens).
* A token belongs to one organization. The organization is resolved FROM THE TOKEN by the narrow SECURITY
  DEFINER lookup -- never from a header, path or query parameter the caller controls.
* Expiry is mandatory and bounded by `SCIM_TOKEN_MAX_TTL_DAYS`. Revocation is a timestamp. Several tokens may be
  active at once so a credential can be rotated with an overlap: `rotate_token` mints the successor and lets the
  old token run on for a short grace period.
* A token never authenticates REST or MCP (those verify signed JWTs; this is an opaque string), and a JWT never
  authenticates SCIM (no row matches its hash).
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.service import record_audit_event
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.scim import repository
from app.core.scim.errors import ScimError
from app.database.models.tenancy_models import ScimToken
from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings
from app.shared.schemas import Identity
from app.shared.security import hash_opaque_token

logger = get_logger(__name__)

TOKEN_PREFIX = "scim_"
_PREFIX_SHOWN = 12
ROTATION_GRACE_HOURS = 24


def generate_raw_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(36)


def hash_token(raw: str) -> str:
    return hash_opaque_token(raw)


@dataclass(frozen=True)
class TokenAuth:
    token_id: uuid.UUID
    organization_id: uuid.UUID


def _unauthorized(reason: str) -> ScimError:
    # One answer for every failure: unknown, malformed, expired and revoked tokens are indistinguishable to the caller.
    logger.warning("scim_token_rejected", reason=reason)
    return ScimError(
        401, "Invalid or expired SCIM credentials.", code="scim.unauthorized",
        headers={"WWW-Authenticate": 'Bearer realm="EKIP SCIM"'},
    )


async def authenticate(session: AsyncSession, raw_token: str, *, now: datetime | None = None) -> TokenAuth:
    """Raw bearer -> the organization it belongs to, or `ScimError(401)`."""
    now = now or datetime.now(timezone.utc)
    if not raw_token.startswith(TOKEN_PREFIX) or len(raw_token) > 200:
        raise _unauthorized("malformed")
    row = await repository.authenticate_token(session, hash_token(raw_token))
    if row is None:
        raise _unauthorized("unknown")
    token_id, organization_id, expires_at, revoked_at = row
    if revoked_at is not None:
        raise _unauthorized("revoked")
    if expires_at <= now:
        raise _unauthorized("expired")
    return TokenAuth(token_id=token_id, organization_id=organization_id)


def _clamp_ttl(ttl_days: int | None) -> int:
    settings = get_settings()
    days = ttl_days if ttl_days is not None else settings.scim_token_default_ttl_days
    if days < 1 or days > settings.scim_token_max_ttl_days:
        raise ValidationError(
            f"A token must expire in 1-{settings.scim_token_max_ttl_days} days.",
            error_code="scim.invalid_token_ttl",
        )
    return days


async def create_token(
    session: AsyncSession,
    actor: Identity,
    *,
    name: str,
    ttl_days: int | None = None,
    now: datetime | None = None,
    audit_action: str = "scim.token_created",
    extra_metadata: dict | None = None,
) -> tuple[ScimToken, str]:
    """Mint a token for `actor.organization_id`. Returns `(row, raw_token)`; the raw value exists only here."""
    now = now or datetime.now(timezone.utc)
    cleaned = name.strip()
    if not cleaned or len(cleaned) > 100:
        raise ValidationError("A token needs a name of 1-100 characters.", error_code="scim.invalid_token_name")
    days = _clamp_ttl(ttl_days)
    organization_id = actor.organization_id
    if await repository.count_active_tokens(session, organization_id, now=now) >= get_settings().scim_max_active_tokens:
        raise ConflictError(
            "This organization already has the maximum number of active SCIM tokens. Revoke one first.",
            error_code="scim.too_many_tokens",
        )
    raw = generate_raw_token()
    row = await repository.insert_token(
        session,
        organization_id=organization_id,
        name=cleaned,
        token_hash=hash_token(raw),
        token_prefix=raw[:_PREFIX_SHOWN],
        created_by=actor.user_id,
        expires_at=now + timedelta(days=days),
    )
    await record_audit_event(
        session, actor, action=audit_action, resource_type="scim_token", resource_id=row.id,
        metadata={"name": cleaned, "expires_at": row.expires_at.isoformat(), "prefix": row.token_prefix,
                  **(extra_metadata or {})},
    )
    return row, raw


async def revoke_token(
    session: AsyncSession, actor: Identity, token_id: uuid.UUID, *, now: datetime | None = None
) -> ScimToken:
    now = now or datetime.now(timezone.utc)
    row = await repository.get_token(session, actor.organization_id, token_id)
    if row is None:
        raise NotFoundError("SCIM token not found.", error_code="scim.token_not_found")
    if row.revoked_at is None:
        row.revoked_at = now
        await session.flush()
        await record_audit_event(
            session, actor, action="scim.token_revoked", resource_type="scim_token", resource_id=row.id,
            metadata={"name": row.name, "prefix": row.token_prefix},
        )
    return row


async def rotate_token(
    session: AsyncSession, actor: Identity, token_id: uuid.UUID, *, ttl_days: int | None = None,
    now: datetime | None = None,
) -> tuple[ScimToken, str]:
    """Mint a successor for `token_id` and let the old token run on for `ROTATION_GRACE_HOURS` (never longer
    than it already had), so the identity provider can be updated without a gap. The old token is NOT revoked
    here: an administrator who wants it dead now revokes it."""
    now = now or datetime.now(timezone.utc)
    old = await repository.get_token(session, actor.organization_id, token_id)
    if old is None:
        raise NotFoundError("SCIM token not found.", error_code="scim.token_not_found")
    if old.revoked_at is not None or old.expires_at <= now:
        raise ConflictError("Only an active token can be rotated.", error_code="scim.token_not_active")
    new_row, raw = await create_token(
        session, actor, name=old.name, ttl_days=ttl_days, now=now, audit_action="scim.token_rotated",
        extra_metadata={"replaces": str(old.id)},
    )
    grace_end = now + timedelta(hours=ROTATION_GRACE_HOURS)
    if grace_end < old.expires_at:
        old.expires_at = grace_end
        await session.flush()
    return new_row, raw
