"""Platform-operator authentication and per-request authorization.

Design (see docs/operations/organization-lifecycle.md):

* A platform operator is an ordinary user account that ALSO has a non-revoked row in
  `platform_operators`. Tenant roles and permissions (`tenancy:manage` included) are never consulted.
* A platform token is a different kind of token from a tenant access token: `type = "platform"`,
  `aud = iss = "ekip-platform"`, no organization, 15 minutes by default, and NO refresh token. The tenant
  verifier rejects it (wrong `type`, and a present `aud`); this verifier rejects every tenant or MCP token
  (wrong `type`/`aud`/`iss`). Neither works in the other's resource.
* The token proves who authenticated, not what they may do: on EVERY request the operator is looked up
  again through `platform_operator_role` (a SECURITY DEFINER function, since the application role has no
  access to the table), so a revoked operator, a deactivated or unverified account, or a password change
  after the token was issued takes effect immediately.
* Every failure a caller can cause at login is the same answer (`platform.invalid_credentials`) and spends
  the same password-hashing work, so it cannot be used to discover which emails belong to operators.
* The kill switch (`PLATFORM_ADMIN_ENABLED`) is checked before anything else, on every entry point.
  Production refuses to boot with it enabled (`Settings`): operator MFA is not implemented yet and
  production access must not exist without it.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from jose import JWTError
from jose import jwt as jose_jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import passwords
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.platform.schemas import (
    PlatformLoginRequest,
    PlatformOperator,
    PlatformSession,
    PlatformTokenClaims,
)
from app.core.users import repository as users_repository
from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings

logger = get_logger(__name__)

PLATFORM_TOKEN_TYPE = "platform"
PLATFORM_AUDIENCE = "ekip-platform"
PLATFORM_ISSUER = "ekip-platform"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_INVALID = "Invalid email or password."


def platform_access_available() -> bool:
    """The kill switch. False (the default) means the platform surface does not exist."""
    return bool(get_settings().platform_admin_enabled)


def assert_platform_enabled() -> None:
    if not platform_access_available():
        raise NotFoundError("Platform access is not available.", error_code="platform.unavailable")


def _invalid_credentials() -> PermissionDeniedError:
    return PermissionDeniedError(_INVALID, error_code="platform.invalid_credentials")


def issue_platform_token(user_id: uuid.UUID) -> tuple[str, datetime, datetime]:
    settings = get_settings()
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(minutes=settings.platform_token_expiry_minutes)
    claims = {
        "sub": str(user_id),
        "type": PLATFORM_TOKEN_TYPE,
        "aud": PLATFORM_AUDIENCE,
        "iss": PLATFORM_ISSUER,
        "iat": int(issued_at.timestamp()),
        "iat_us": (issued_at - _EPOCH) // timedelta(microseconds=1),
        "exp": int(expires_at.timestamp()),
    }
    token = jose_jwt.encode(claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, issued_at, expires_at


def verify_platform_token(token: str) -> PlatformTokenClaims:
    """Verify a platform token's signature, expiry, type, audience and issuer. No database access."""
    settings = get_settings()
    try:
        claims = jose_jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            audience=PLATFORM_AUDIENCE,
            issuer=PLATFORM_ISSUER,
        )
    except JWTError as exc:
        raise PermissionDeniedError(
            "Invalid or expired platform token.", error_code="platform.invalid_token"
        ) from exc
    # Strict, unlike the tenant verifier's backward-compatible default: this kind of token has no legacy.
    if claims.get("type") != PLATFORM_TOKEN_TYPE or claims.get("aud") != PLATFORM_AUDIENCE:
        raise PermissionDeniedError(
            "Invalid or expired platform token.", error_code="platform.invalid_token"
        )
    try:
        iat_us = claims.get("iat_us")
        if isinstance(iat_us, int) and not isinstance(iat_us, bool):
            issued_at = _EPOCH + timedelta(microseconds=iat_us)
        else:
            issued_at = datetime.fromtimestamp(claims["iat"], tz=timezone.utc)
        return PlatformTokenClaims(
            user_id=uuid.UUID(claims["sub"]),
            issued_at=issued_at,
            expires_at=datetime.fromtimestamp(claims["exp"], tz=timezone.utc),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise PermissionDeniedError(
            "Invalid or expired platform token.", error_code="platform.invalid_token"
        ) from exc


async def operator_role(session: AsyncSession, user_id: uuid.UUID) -> str | None:
    """The user's CURRENT platform role (`admin`/`viewer`), or None if they are not an active operator."""
    result = await session.execute(
        text("SELECT public.platform_operator_role(:user_id)"), {"user_id": user_id}
    )
    return result.scalar_one_or_none()


async def login(
    session: AsyncSession, data: PlatformLoginRequest, *, request_id: str | None = None
) -> PlatformSession:
    """Email + password -> a short-lived platform access token. Every refusal is the same error."""
    assert_platform_enabled()
    email = data.email.strip()
    user = await users_repository.get_by_email_ci(session, email)
    if user is None or user.password_hash is None:
        await asyncio.to_thread(passwords.verify_against_dummy, data.password)
        logger.warning("platform_login_failed", reason="unknown_or_no_password")
        raise _invalid_credentials()
    if not await asyncio.to_thread(passwords.verify_password, data.password, user.password_hash):
        logger.warning("platform_login_failed", reason="wrong_password", user_id=str(user.id))
        raise _invalid_credentials()
    role = await operator_role(session, user.id)  # also requires an active, verified account
    if role is None:
        logger.warning("platform_login_failed", reason="not_an_active_operator", user_id=str(user.id))
        raise _invalid_credentials()

    await session.execute(
        text("SELECT public.platform_record_login(:user_id, :request_id)"),
        {"user_id": user.id, "request_id": request_id},
    )
    token, issued_at, expires_at = issue_platform_token(user.id)
    logger.info("platform_login", user_id=str(user.id), role=role)
    return PlatformSession(
        access_token=token,
        expires_in=int((expires_at - issued_at).total_seconds()),
        role=role,  # type: ignore[arg-type]
    )


async def authenticate(session: AsyncSession, token: str) -> PlatformOperator:
    """Token -> the CURRENT operator, or refuse. The checks are, in order: kill switch, token, account
    still valid, password not changed since the token was issued, operator grant still active."""
    assert_platform_enabled()
    claims = verify_platform_token(token)
    user = await users_repository.get_by_id(session, claims.user_id)
    if user is None or not user.is_active or user.email_verified_at is None:
        raise PermissionDeniedError("Platform access denied.", error_code="platform.access_denied")
    if user.sessions_valid_after is not None and claims.issued_at < user.sessions_valid_after:
        raise PermissionDeniedError(
            "This session was ended. Sign in again.", error_code="platform.session_revoked"
        )
    role = await operator_role(session, user.id)
    if role is None:
        raise PermissionDeniedError("Platform access denied.", error_code="platform.access_denied")
    return PlatformOperator(
        user_id=user.id, email=user.email, display_name=user.display_name, role=role  # type: ignore[arg-type]
    )
