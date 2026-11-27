"""Server-side email-ownership verification (the only thing that may turn an
unproven email address into an identity EKIP will authenticate or provision).

Owned by: core/auth. Design and rationale: docs/security/email-verification-*.md.

Invariants this module enforces, and that tests pin:

* **Nothing exists before proof.** A self-service signup (`start_registration`)
  and a password invitation acceptance (`start_invitation_acceptance`) only
  write an `email_verifications` row. The organization, user, role, session
  and invitation consumption happen inside `verify_token`, in the same
  transaction that consumes the token.
* **Tokens**: 256 random bits (`generate_opaque_token`), SHA-256 at rest,
  expiring, single use. The raw token exists in memory only between
  generation and the outgoing mail; it is never logged, stored or returned
  by any API.
* **Atomic single use**: `repository.consume_verification_by_hash` is one
  `UPDATE ... WHERE consumed_at IS NULL AND expires_at > now() RETURNING`.
  Activation runs in the same transaction, so a failed activation rolls the
  consumption back (a transient error never burns the link).
* **No enumeration**: `start_registration` / `resend_verification` return
  `None` -- indistinguishable to the caller -- when the address already has
  an account, is rate limited, or has nothing to resend.
* **Sending is rate limited in the database** (per-row cooldown and daily
  cap), independent of the Redis limiter, which fails open.
* **No automatic authentication**: nothing here returns a session.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import repository
from app.core.exceptions import ConflictError, PermissionDeniedError, ValidationError
from app.core.tenancy import repository as tenancy_repository
from app.core.tenancy import service as tenancy_service
from app.core.tenancy.schemas import OrganizationCreate
from app.core.users import service as users_service
from app.database.session import set_tenant_context
from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings
from app.shared.organization_status import is_suspended
from app.shared.email import EmailDeliveryError, EmailNotConfiguredError, get_email_sender
from app.shared.email.messages import EmailSender
from app.shared.email.templates import render_verification_email, verification_link
from app.shared.security import generate_opaque_token, hash_opaque_token

logger = get_logger(__name__)

KIND_REGISTRATION = "registration"
KIND_INVITATION = "invitation"
KIND_EXISTING_USER = "existing_user"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAX_EMAIL_LENGTH = 254
_SEND_WINDOW = timedelta(hours=24)
_DELIVERY_ATTEMPTS = 3


def normalize_email(raw: str) -> str:
    """Canonical form for every NEW identity path: stripped, lower-cased,
    single `@`, no whitespace/control characters, <= 254 chars."""
    email = raw.strip().lower()
    if (
        not email
        or len(email) > _MAX_EMAIL_LENGTH
        or not _EMAIL_RE.match(email)
        or any(ord(ch) < 32 for ch in email)
    ):
        raise ValidationError("Enter a valid email address.", error_code="auth.invalid_email")
    return email


@dataclass(frozen=True)
class PendingDelivery:
    """A verification mail that must now be sent. Carries the raw token, so it
    is deliberately never logged: `raw_token` is excluded from `repr`."""

    verification_id: uuid.UUID
    kind: str
    email: str
    raw_token: str = field(repr=False)


@dataclass(frozen=True)
class VerificationResult:
    kind: str
    status: str = "verified"


# --- issuing ------------------------------------------------------------------


def _may_send(row, now: datetime) -> bool:
    settings = get_settings()
    cooldown = timedelta(seconds=settings.email_verification_resend_cooldown_seconds)
    if row.last_sent_at is not None and now - row.last_sent_at < cooldown:
        return False
    window_open = (
        row.send_window_started_at is not None and now - row.send_window_started_at < _SEND_WINDOW
    )
    sent_in_window = row.send_count if window_open else 0
    return sent_in_window < settings.email_verification_max_sends_per_day


def _rotate(row, *, raw_token: str, now: datetime, payload: dict | None) -> None:
    settings = get_settings()
    window_open = (
        row.send_window_started_at is not None and now - row.send_window_started_at < _SEND_WINDOW
    )
    if not window_open:
        row.send_window_started_at = now
        row.send_count = 0
    row.send_count += 1
    row.last_sent_at = now
    row.token_hash = hash_opaque_token(raw_token)  # the previous token is now dead
    row.expires_at = now + timedelta(minutes=settings.email_verification_token_ttl_minutes)
    if payload:
        for key, value in payload.items():
            setattr(row, key, value)


async def _issue_challenge(
    session: AsyncSession,
    *,
    kind: str,
    email: str,
    payload: dict,
    now: datetime,
    overwrite_payload: bool,
    create_if_missing: bool,
) -> PendingDelivery | None:
    """Create or rotate the single open challenge for (email, kind).

    Returns the delivery to perform, or `None` if rate limited / nothing to
    do. Row-locks the existing challenge (`FOR UPDATE`) so concurrent
    requests serialise on the cooldown bookkeeping.
    """
    settings = get_settings()
    raw_token = generate_opaque_token()
    row = await repository.get_open_verification_for_update(session, email=email, kind=kind)

    if row is None:
        if not create_if_missing:
            return None
        new_id = await repository.insert_open_verification_if_absent(
            session,
            values={
                "kind": kind,
                "email": email,
                "token_hash": hash_opaque_token(raw_token),
                "expires_at": now + timedelta(minutes=settings.email_verification_token_ttl_minutes),
                "last_sent_at": now,
                "send_count": 1,
                "send_window_started_at": now,
                **payload,
            },
        )
        if new_id is not None:
            return PendingDelivery(new_id, kind, email, raw_token)
        # A concurrent request created it first: fall through and treat it as existing.
        row = await repository.get_open_verification_for_update(session, email=email, kind=kind)
        if row is None:
            return None

    if not _may_send(row, now):
        logger.info("email_verification_send_suppressed", verification_id=str(row.id), kind=kind)
        return None
    _rotate(row, raw_token=raw_token, now=now, payload=payload if overwrite_payload else None)
    await session.flush()
    return PendingDelivery(row.id, kind, email, raw_token)


async def start_registration(
    session: AsyncSession,
    *,
    email: str,
    display_name: str,
    password_hash: str,
    organization_name: str,
    organization_slug: str,
    now: datetime | None = None,
) -> PendingDelivery | None:
    """Record a pending signup. Creates NO organization, user, role or session.

    Returns the mail to send, or `None` when nothing should be sent -- the
    address already has an account, or sending is rate limited -- which the
    caller must present identically to the success case.

    The slug check runs FIRST and independently of the email: if it were
    skipped for existing emails, `409 slug_taken` vs `202` would reveal
    whether the email has an account. Org slugs are already publicly probeable
    (`GET /auth/{slug}/login`), so reporting a taken slug leaks nothing new.
    Last signup wins for a pending address, so an attacker pre-registering a
    victim's address cannot block the real owner.
    """
    now = now or datetime.now(timezone.utc)
    email = normalize_email(email)
    OrganizationCreate(name=organization_name, slug=organization_slug)  # validate early

    if await tenancy_repository.get_organization_by_slug(session, organization_slug) is not None:
        raise ConflictError(
            "An organization with this slug already exists.",
            error_code="organization.slug_taken",
            detail={"slug": organization_slug},
        )
    if await users_service.find_user_id_by_email_ci(session, email) is not None:
        logger.info("registration_suppressed_existing_account")
        return None

    return await _issue_challenge(
        session,
        kind=KIND_REGISTRATION,
        email=email,
        payload={
            "display_name": display_name,
            "password_hash": password_hash,
            "organization_name": organization_name,
            "organization_slug": organization_slug,
        },
        now=now,
        overwrite_payload=True,
        create_if_missing=True,
    )


async def start_invitation_acceptance(
    session: AsyncSession,
    *,
    invitation_id: uuid.UUID,
    email: str,
    display_name: str,
    password_hash: str,
    now: datetime | None = None,
) -> PendingDelivery | None:
    """Record a pending password-invitation acceptance (the caller has already
    validated the invitation token, status and expiry). The invitation stays
    `pending` and no user exists until `verify_token`."""
    now = now or datetime.now(timezone.utc)
    return await _issue_challenge(
        session,
        kind=KIND_INVITATION,
        email=normalize_email(email),
        payload={
            "invitation_id": invitation_id,
            "display_name": display_name,
            "password_hash": password_hash,
        },
        now=now,
        overwrite_payload=True,
        create_if_missing=True,
    )


async def resend_verification(
    session: AsyncSession, *, email: str, now: datetime | None = None
) -> PendingDelivery | None:
    """Resend for whatever is outstanding for `email`. Always safe to present
    as "if applicable, a message was sent": returns `None` for an unknown
    address, an already-verified or inactive account, or a rate-limited send.

    Order: an open registration, an open invitation acceptance, then an
    unverified existing account. Rotating the token invalidates the previous
    one immediately.
    """
    now = now or datetime.now(timezone.utc)
    try:
        email = normalize_email(email)
    except ValidationError:
        return None

    for kind in (KIND_REGISTRATION, KIND_INVITATION):
        delivery = await _issue_challenge(
            session,
            kind=kind,
            email=email,
            payload={},
            now=now,
            overwrite_payload=False,
            create_if_missing=False,
        )
        if delivery is not None:
            return delivery
        if (
            await repository.get_open_verification_for_update(session, email=email, kind=kind)
            is not None
        ):
            return None  # exists but suppressed (rate limited): do not fall through

    state = await users_service.get_user_email_state(session, email)
    if state is None or not state.is_active or state.email_verified_at is not None:
        return None
    return await _issue_challenge(
        session,
        kind=KIND_EXISTING_USER,
        email=email,
        payload={"user_id": state.user_id},
        now=now,
        overwrite_payload=True,
        create_if_missing=True,
    )


# --- consuming ----------------------------------------------------------------


async def verify_token(session: AsyncSession, raw_token: str) -> VerificationResult:
    """Consume `raw_token` and complete whatever it was issued for.

    The consume statement and the activation share the caller's transaction.
    Any exception here propagates, `get_db_session` rolls back, and the token
    is therefore NOT consumed by a failed activation. Never returns tokens:
    the user signs in through the normal login endpoint afterwards.
    """
    now = datetime.now(timezone.utc)
    token_hash = hash_opaque_token(raw_token.strip())

    row = await repository.consume_verification_by_hash(session, token_hash=token_hash, now=now)
    if row is None:
        existing = await repository.get_verification_by_hash(session, token_hash)
        if existing is None:
            raise ValidationError(
                "This verification link is not valid.", error_code="email_verification.invalid"
            )
        if existing.consumed_at is not None:
            raise ConflictError(
                "This verification link has already been used.",
                error_code="email_verification.already_used",
            )
        raise ValidationError(
            "This verification link has expired. Request a new one.",
            error_code="email_verification.expired",
        )

    if row.kind == KIND_REGISTRATION:
        await _activate_registration(session, row)
    elif row.kind == KIND_INVITATION:
        await _activate_invitation(session, row, now)
    elif row.kind == KIND_EXISTING_USER:
        await _activate_existing_user(session, row)
    else:  # pragma: no cover - the CHECK constraint makes this unreachable
        raise ValidationError("Unknown verification type.", error_code="email_verification.invalid")

    logger.info("email_verified", verification_id=str(row.id), kind=row.kind)
    return VerificationResult(kind=row.kind)


async def _activate_registration(session: AsyncSession, row) -> None:
    # The creator just proved ownership of their email address, which is the only thing an
    # organization created through self-service signup waits for: it starts `active`.
    organization = await tenancy_service.create_organization(
        session,
        OrganizationCreate(name=row.organization_name, slug=row.organization_slug),
        initial_status="active",
    )
    user_id = await users_service.create_verified_user(
        session,
        email=row.email,
        display_name=row.display_name,
        password_hash=row.password_hash,
        via="email_link",
    )
    role_id = await users_service.ensure_admin_role(session)
    # `user_roles` is FORCE RLS: the INSERT is only allowed under the NEW
    # organization's tenant context (set explicitly, not relying on a side
    # effect of `create_organization`).
    await set_tenant_context(session, organization.id)
    await users_service.assign_role(
        session, user_id=user_id, organization_id=organization.id, role_id=role_id
    )
    logger.info(
        "registration_activated", user_id=str(user_id), organization_id=str(organization.id)
    )


async def _activate_invitation(session: AsyncSession, row, now: datetime) -> None:
    organization_id = await repository.resolve_invitation_organization_id(
        session, row.invitation_id
    )
    if organization_id is None:
        raise ConflictError("Invitation not found.", error_code="invitation.not_found")
    await set_tenant_context(session, organization_id)
    organization = await tenancy_repository.get_organization_by_id(session, organization_id)
    if organization is None or is_suspended(organization.status):
        # Raising rolls the whole activation back: the link is not consumed and no user,
        # role or invitation change is written.
        raise PermissionDeniedError(
            "This organization's access has been suspended. Contact your administrator.",
            error_code="auth.organization_suspended",
        )
    invitation = await tenancy_repository.get_invitation_by_id(session, row.invitation_id)
    if invitation is None:
        raise ConflictError("Invitation not found.", error_code="invitation.not_found")
    if invitation.status != "pending":
        raise ConflictError(
            "This invitation is no longer pending.", error_code="invitation.not_pending"
        )
    if invitation.expires_at <= now:
        raise ConflictError("This invitation has expired.", error_code="invitation.expired")
    if invitation.email.strip().lower() != row.email:
        raise ConflictError("Invitation does not match this email.", error_code="invitation.not_found")

    user_id = await users_service.create_verified_user(
        session,
        email=row.email,
        display_name=row.display_name,
        password_hash=row.password_hash,
        via="email_link",
    )
    await users_service.assign_role(
        session,
        user_id=user_id,
        organization_id=organization_id,
        role_id=invitation.grants_role_id,
    )
    await tenancy_service.accept_invitation(session, invitation.id)
    logger.info(
        "invitation_activated",
        user_id=str(user_id),
        organization_id=str(organization_id),
        invitation_id=str(invitation.id),
    )


async def _activate_existing_user(session: AsyncSession, row) -> None:
    state = await users_service.get_user_email_state_by_id(session, row.user_id)
    if state is None or not state.is_active or state.email.strip().lower() != row.email:
        raise ConflictError(
            "This verification link can no longer be used.",
            error_code="email_verification.invalid",
        )
    await users_service.mark_email_verified(session, user_id=state.user_id, via="email_link")
    # Whatever sessions existed while ownership was unproven (legacy accounts,
    # possibly squatter-held) are revoked across every organization: the
    # person who just proved the mailbox signs in fresh.
    now = datetime.now(timezone.utc)
    for organization_id in await users_service.list_organizations_for_login(
        session, state.user_id
    ):
        await set_tenant_context(session, organization_id)
        await repository.revoke_all_for_user(
            session, state.user_id, organization_id, revoked_at=now
        )


# --- delivery -----------------------------------------------------------------


async def deliver_verification_email(
    delivery: PendingDelivery,
    *,
    sender: EmailSender | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    """Send the mail after the HTTP response. Bounded retry with exponential
    backoff; returns whether it was accepted by the provider. Failure is never
    reported as success: it is logged at `error` (row id and kind only -- never
    the address, link or token) and the user can request a resend. Safe to run
    twice: both mails carry the same single-use token, the second use is
    rejected as already used.
    """
    settings = get_settings()
    link = verification_link(settings.frontend_public_base_url, delivery.raw_token)
    message = render_verification_email(
        to=delivery.email,
        link=link,
        ttl_minutes=settings.email_verification_token_ttl_minutes,
        kind=delivery.kind,
    )
    for attempt in range(1, _DELIVERY_ATTEMPTS + 1):
        try:
            active_sender = sender or get_email_sender()
            await active_sender.send(message)
            logger.info(
                "verification_email_sent",
                verification_id=str(delivery.verification_id),
                kind=delivery.kind,
                attempt=attempt,
            )
            return True
        except EmailNotConfiguredError:
            break  # retrying cannot help
        except EmailDeliveryError as exc:
            logger.warning(
                "verification_email_attempt_failed",
                verification_id=str(delivery.verification_id),
                kind=delivery.kind,
                attempt=attempt,
                error=str(exc),
            )
            if attempt < _DELIVERY_ATTEMPTS:
                await sleep(2**attempt)
    logger.error(
        "verification_email_delivery_failed",
        verification_id=str(delivery.verification_id),
        kind=delivery.kind,
    )
    return False


async def purge_stale(
    session: AsyncSession, *, now: datetime | None = None, retention: timedelta = timedelta(days=7)
) -> int:
    """Delete challenges that expired or were consumed more than `retention`
    ago (abandoned signups). Never touches users/organizations."""
    now = now or datetime.now(timezone.utc)
    return await repository.delete_stale_verifications(
        session, expired_before=now - retention, consumed_before=now - retention
    )
