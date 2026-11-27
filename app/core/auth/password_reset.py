"""Self-service "forgot password" and the credential-change plumbing around it.

Owned by: core/auth. Mirrors `core.auth.email_verification` (same token and
single-use design) so there is ONE mental model for "an emailed link proves you
control the mailbox".

Invariants this module enforces, and that tests pin:

* **No enumeration.** `request_reset` returns `None` -- indistinguishable to the
  caller -- for an unknown address, an SSO-only account, an inactive or
  unverified account, and a rate-limited one. The HTTP layer always answers the
  same 202.
* **Tokens**: 256 random bits, SHA-256 at rest, 30-minute default life, single
  use via one atomic `UPDATE ... RETURNING`; the raw token exists in memory only
  between generation and the outgoing mail and is never logged.
* **Sending is rate limited in the database** (per-account cooldown and daily
  cap), independent of the Redis limiter, which fails open.
* **A reset never creates a credential.** Accounts without a password (SSO
  only) get no link and cannot be given one here -- a password would bypass the
  identity provider's policy and offboarding.
* **A reset never counts as email verification.** An account whose address was
  never verified gets no link; it must verify first.
* **A reset ends every session, everywhere**: the password, the session cut-off
  (`users.sessions_valid_after`) and the revocation of all refresh tokens in
  all organizations happen in ONE transaction with the token consumption, so a
  failure anywhere un-burns the link and leaves nothing half-done. It does not
  log anyone in, and it does not touch any second factor.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import passwords, repository
from app.core.auth.email_verification import normalize_email
from app.core.auth.security_events import audit_user_event, revoke_all_sessions_everywhere
from app.core.exceptions import ConflictError, ValidationError
from app.core.users import service as users_service
from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings
from app.shared.email import EmailDeliveryError, EmailNotConfiguredError, get_email_sender
from app.shared.email.messages import EmailSender
from app.shared.email.templates import (
    password_reset_link,
    render_password_changed_email,
    render_password_reset_email,
)
from app.shared.security import generate_opaque_token, hash_opaque_token

logger = get_logger(__name__)

_SEND_WINDOW = timedelta(hours=24)
_DELIVERY_ATTEMPTS = 3


@dataclass(frozen=True)
class PendingResetDelivery:
    """A reset mail that must now be sent. Carries the raw token, so it is
    deliberately never logged: `raw_token` is excluded from `repr`."""

    reset_id: uuid.UUID
    email: str
    raw_token: str = field(repr=False)


@dataclass(frozen=True)
class PendingPasswordNotice:
    """The "your password was changed" notice to send after commit (no link in it)."""

    email: str = field(repr=False)
    how: str  # "reset" | "change"
    when: datetime


# --- requesting ------------------------------------------------------------------------


def _may_send(row, now: datetime) -> bool:
    settings = get_settings()
    cooldown = timedelta(seconds=settings.password_reset_resend_cooldown_seconds)
    if row.last_sent_at is not None and now - row.last_sent_at < cooldown:
        return False
    window_open = (
        row.send_window_started_at is not None and now - row.send_window_started_at < _SEND_WINDOW
    )
    sent_in_window = row.send_count if window_open else 0
    return sent_in_window < settings.password_reset_max_sends_per_day


def _rotate(row, *, raw_token: str, now: datetime) -> None:
    settings = get_settings()
    window_open = (
        row.send_window_started_at is not None and now - row.send_window_started_at < _SEND_WINDOW
    )
    if not window_open:
        row.send_window_started_at = now
        row.send_count = 0
    row.send_count += 1
    row.last_sent_at = now
    row.token_hash = hash_opaque_token(raw_token)  # the previous link is now dead
    row.expires_at = now + timedelta(minutes=settings.password_reset_token_ttl_minutes)


async def _issue_link(
    session: AsyncSession, *, user_id: uuid.UUID, email: str, now: datetime
) -> PendingResetDelivery | None:
    settings = get_settings()
    raw_token = generate_opaque_token()
    row = await repository.get_open_reset_for_update(session, user_id=user_id)
    if row is None:
        new_id = await repository.insert_open_reset_if_absent(
            session,
            values={
                "user_id": user_id,
                "token_hash": hash_opaque_token(raw_token),
                "expires_at": now + timedelta(minutes=settings.password_reset_token_ttl_minutes),
                "last_sent_at": now,
                "send_count": 1,
                "send_window_started_at": now,
            },
        )
        if new_id is not None:
            return PendingResetDelivery(new_id, email, raw_token)
        row = await repository.get_open_reset_for_update(session, user_id=user_id)
        if row is None:
            return None
    if not _may_send(row, now):
        logger.info("password_reset_send_suppressed", reset_id=str(row.id))
        return None
    _rotate(row, raw_token=raw_token, now=now)
    await session.flush()
    return PendingResetDelivery(row.id, email, raw_token)


async def request_reset(
    session: AsyncSession, *, email: str, now: datetime | None = None
) -> PendingResetDelivery | None:
    """Start a reset for `email`, or do nothing -- the caller cannot tell which.

    Eligible only if the account exists, is active, has a local password and has a
    verified email address. Returns the mail to send, or `None`.
    """
    now = now or datetime.now(timezone.utc)
    try:
        normalized = normalize_email(email)
    except ValidationError:
        return None
    account = await users_service.get_credential_lookup_by_email_ci(session, normalized)
    if account is None:
        reason = "no_account"
    elif not account.is_active:
        reason = "inactive"
    elif account.password_hash is None:  # SSO-only: never gets a password through this flow
        reason = "no_password"
    elif account.email_verified_at is None:  # a reset is not a verification
        reason = "email_unverified"
    else:
        reason = None
    if reason is not None:
        # Server log only -- the HTTP answer is identical for every reason (no enumeration), and
        # this line carries neither the address nor any identifier of the account.
        logger.info("password_reset_not_eligible", reason=reason)
        return None

    delivery = await _issue_link(session, user_id=account.user_id, email=normalized, now=now)
    if delivery is not None:
        await audit_user_event(
            session, user_id=account.user_id, action="user.password_reset_requested"
        )
    return delivery


# --- completing ------------------------------------------------------------------------


async def reset_password(
    session: AsyncSession, *, token: str, new_password: str
) -> PendingPasswordNotice:
    """Consume `token` and set `new_password`, ending every session of the account.

    Everything below shares the caller's transaction: any exception rolls the whole
    thing back, including the consumption of the link (a transient failure never
    burns it). The password policy is checked BEFORE the token is consumed, so a
    too-weak password does not cost the person their link.
    """
    passwords.validate_new_password(new_password)
    new_hash = await asyncio.to_thread(passwords.hash_password, new_password)

    now = datetime.now(timezone.utc)
    token_hash = hash_opaque_token(token.strip())
    row = await repository.consume_reset_by_hash(session, token_hash=token_hash, now=now)
    if row is None:
        existing = await repository.get_reset_by_hash(session, token_hash)
        if existing is None:
            raise ValidationError(
                "This password reset link is not valid.", error_code="auth.reset_token_invalid"
            )
        if existing.consumed_at is not None:
            raise ConflictError(
                "This password reset link has already been used.",
                error_code="auth.reset_token_used",
            )
        raise ValidationError(
            "This password reset link has expired. Request a new one.",
            error_code="auth.reset_token_expired",
        )

    account = await users_service.get_credential_lookup_by_id(session, row.user_id)
    if (
        account is None
        or not account.is_active
        or account.password_hash is None
        or account.email_verified_at is None
    ):
        # The account changed after the link was issued (deactivated, SSO-only, ...).
        raise ValidationError(
            "This password reset link is not valid.", error_code="auth.reset_token_invalid"
        )
    passwords.validate_new_password(new_password, email=account.email)

    cutoff = datetime.now(timezone.utc)
    await users_service.replace_password(
        session, user_id=account.user_id, password_hash=new_hash, sessions_valid_after=cutoff
    )
    await repository.consume_open_resets_for_user(session, user_id=account.user_id, now=cutoff)
    await revoke_all_sessions_everywhere(
        session,
        user_id=account.user_id,
        revoked_at=cutoff,
        audit_action="user.password_reset_completed",
    )
    logger.info("password_reset_completed", user_id=str(account.user_id))
    return PendingPasswordNotice(email=account.email or "", how="reset", when=cutoff)


# --- delivery --------------------------------------------------------------------------


async def deliver_reset_email(
    delivery: PendingResetDelivery,
    *,
    sender: EmailSender | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    """Send the reset mail after the HTTP response. Bounded retry with backoff;
    returns whether the provider accepted it. Failure is never reported as success
    and is logged without the address, link or token."""
    settings = get_settings()
    message = render_password_reset_email(
        to=delivery.email,
        link=password_reset_link(settings.frontend_public_base_url, delivery.raw_token),
        ttl_minutes=settings.password_reset_token_ttl_minutes,
    )
    for attempt in range(1, _DELIVERY_ATTEMPTS + 1):
        try:
            active_sender = sender or get_email_sender()
            await active_sender.send(message)
            logger.info("password_reset_email_sent", reset_id=str(delivery.reset_id), attempt=attempt)
            return True
        except EmailNotConfiguredError:
            break
        except EmailDeliveryError as exc:
            logger.warning(
                "password_reset_email_attempt_failed",
                reset_id=str(delivery.reset_id),
                attempt=attempt,
                error=str(exc),
            )
            if attempt < _DELIVERY_ATTEMPTS:
                await sleep(2**attempt)
    logger.error("password_reset_email_delivery_failed", reset_id=str(delivery.reset_id))
    return False


async def deliver_password_changed_notice(
    notice: PendingPasswordNotice,
    *,
    sender: EmailSender | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    """Best-effort security notice (single retry). It carries no link."""
    if not notice.email:
        return False
    message = render_password_changed_email(to=notice.email, how=notice.how, when=notice.when)
    for attempt in range(1, 3):
        try:
            await (sender or get_email_sender()).send(message)
            return True
        except EmailNotConfiguredError:
            return False
        except EmailDeliveryError as exc:
            logger.warning("password_changed_notice_attempt_failed", attempt=attempt, error=str(exc))
            if attempt < 2:
                await sleep(2)
    logger.error("password_changed_notice_failed")
    return False


async def purge_stale(
    session: AsyncSession, *, now: datetime | None = None, retention: timedelta = timedelta(days=7)
) -> int:
    """Delete links that expired or were consumed more than `retention` ago."""
    now = now or datetime.now(timezone.utc)
    return await repository.delete_stale_resets(
        session, expired_before=now - retention, consumed_before=now - retention
    )
