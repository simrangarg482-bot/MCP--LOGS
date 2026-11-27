"""Emailing an invitation link to the invited address.

Owned by: core/tenancy. Reuses EKIP's existing email stack -- `app.shared.email`
(SMTP sender, message type, templates) and the same bounded retry-with-backoff
shape as `core.auth.email_verification.deliver_verification_email` -- rather
than introducing another provider.

Two halves, deliberately separate:

* `prepare_invitation_email` runs INSIDE the request, on the request's session
  and tenant context. It reads the organization, inviter, role and the role's
  permissions and renders the message, so what the recipient is told matches
  what the database says at that moment.
* `deliver_invitation_email` runs AFTER the response, as a background task. It
  holds no request session (that one is closed): it sends, then opens its OWN
  session, sets the invitation's organization as the tenant context (the
  `invitations` table is RLS-protected, so without it the update would match no
  row) and records the outcome.

Honest status: `sent` is written only after the SMTP provider accepted the
message. Exhausted retries record `failed`; no provider, `not_configured`. The
recording is conditional on the invitation still carrying the token this task
was sending, so a resend (which rotates the token) can never be overwritten by
a late task for the old link.

Nothing here ever logs a token, link, recipient or message body.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.service import record_audit_event
from app.core.tenancy import repository
from app.core.users import role_repository
from app.core.users import service as users_service
from app.core.users.permissions import permission_spec
from app.database.models.tenancy_models import Invitation as InvitationRow
from app.database.session import session_scope, set_tenant_context
from app.shared.organization_status import is_suspended
from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings
from app.shared.email import (
    EmailDeliveryError,
    EmailMessage,
    EmailNotConfiguredError,
    EmailSender,
    get_email_sender,
)
from app.shared.email.templates import invitation_link, render_invitation_email
from app.shared.schemas import Identity

logger = get_logger(__name__)

#: Same bounded retry as verification mail (`email_verification._DELIVERY_ATTEMPTS`).
DELIVERY_ATTEMPTS = 3

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True)
class PendingInvitationEmail:
    """Everything the background task needs; carries no session and no raw
    token (the link is already inside `message`)."""

    invitation_id: uuid.UUID
    organization_id: uuid.UUID
    #: Identity of the admin who triggered it, kept only to attribute the audit entry.
    actor: Identity
    message: EmailMessage
    #: Hash of the token the link in `message` carries -- see `record_invitation_email_result`.
    token_hash: str
    #: `invitation.email_sent` / `invitation.email_failed` metadata: "create" or "resend".
    origin: str

    def __repr__(self) -> str:  # never print the message (it holds the link)
        return f"PendingInvitationEmail(invitation_id={self.invitation_id}, origin={self.origin})"


async def prepare_invitation_email(
    session: AsyncSession,
    actor: Identity,
    row: InvitationRow,
    raw_token: str,
    *,
    origin: str,
) -> PendingInvitationEmail:
    """Render the invitation mail for `row` (whose token hash is already
    stored). Must run under the invitation's organization tenant context."""
    settings = get_settings()
    organization = await repository.get_organization_by_id(session, row.organization_id)
    inviter_name = await users_service.get_display_name(session, row.invited_by)
    role = await role_repository.get_visible_role(session, row.organization_id, row.grants_role_id)
    codes = (
        (await role_repository.permission_codes_by_role(session, [row.grants_role_id])).get(
            row.grants_role_id, ()
        )
        if role is not None
        else ()
    )
    permissions = []
    for code in codes:
        spec = permission_spec(code)
        permissions.append((spec.label, spec.description) if spec else (code, ""))

    base = settings.frontend_public_base_url
    if not base.lower().startswith("https://"):
        # Not an error (local development), but such a link only works on the
        # machine running the frontend -- say so loudly rather than let an
        # invitee discover it.
        logger.warning(
            "invitation_link_base_url_not_public",
            hint="Set FRONTEND_PUBLIC_BASE_URL to a publicly reachable https:// URL before real use.",
        )
    message = render_invitation_email(
        to=row.email,
        link=invitation_link(base, row.id, raw_token),
        inviter_name=inviter_name or "",
        organization_name=organization.name if organization else "",
        role_name=role.name if role else "your assigned role",
        role_description=role.description if role else None,
        permissions=permissions,
        expires_at=row.expires_at,
    )
    assert row.token_hash is not None
    return PendingInvitationEmail(
        invitation_id=row.id,
        organization_id=row.organization_id,
        actor=actor,
        message=message,
        token_hash=row.token_hash,
        origin=origin,
    )


async def deliver_invitation_email(
    pending: PendingInvitationEmail,
    *,
    sender: EmailSender | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    session_factory: SessionFactory = session_scope,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> str:
    """Send `pending`, then record the outcome in a fresh tenant-scoped session.
    Returns the recorded status (`sent` / `failed` / `not_configured`).

    This runs AFTER the request that created the invitation, outside `resolve_identity`. If the
    organization was suspended in the meantime (a window of seconds), nothing is sent and the
    invitation is recorded as `failed` with zero attempts. A status lookup that itself ERRORS does
    not block the send: the email was authorized when the request ran, and a database blip must not
    silently drop it -- only a confirmed suspension stops it."""
    if await _organization_is_suspended(pending.organization_id, session_factory):
        logger.warning(
            "invitation_email_skipped_organization_suspended",
            invitation_id=str(pending.invitation_id),
        )
        await _record_outcome(pending, status="failed", attempts=0, session_factory=session_factory, now=now)
        return "failed"

    status = "failed"
    attempts = 0
    for attempt in range(1, DELIVERY_ATTEMPTS + 1):
        attempts = attempt
        try:
            active_sender = sender or get_email_sender()
            await active_sender.send(pending.message)
            status = "sent"
            break
        except EmailNotConfiguredError:
            status = "not_configured"
            break  # retrying cannot help
        except EmailDeliveryError as exc:
            logger.warning(
                "invitation_email_attempt_failed",
                invitation_id=str(pending.invitation_id),
                attempt=attempt,
                error=str(exc),  # class name only -- never the recipient or body
            )
            if attempt < DELIVERY_ATTEMPTS:
                await sleep(2**attempt)

    await _record_outcome(pending, status=status, attempts=attempts, session_factory=session_factory, now=now)
    log = logger.info if status == "sent" else logger.error
    log("invitation_email_" + status, invitation_id=str(pending.invitation_id), attempts=attempts)
    return status


async def _organization_is_suspended(
    organization_id: uuid.UUID, session_factory: SessionFactory
) -> bool:
    try:
        async with session_factory() as session:
            _exists, status = await repository.get_organization_status(session, organization_id)
        return is_suspended(status)
    except Exception:  # noqa: BLE001 -- see `deliver_invitation_email`: fail open on a lookup error
        logger.warning("invitation_email_status_lookup_failed", exc_info=True)
        return False


async def _record_outcome(
    pending: PendingInvitationEmail,
    *,
    status: str,
    attempts: int,
    session_factory: SessionFactory,
    now: Callable[[], datetime],
) -> None:
    try:
        async with session_factory() as session:
            # `invitations` is RLS-protected: without this the UPDATE matches nothing.
            await set_tenant_context(session, pending.organization_id)
            updated = await repository.record_invitation_email_result(
                session,
                pending.invitation_id,
                expected_token_hash=pending.token_hash,
                status=status,
                attempts=attempts,
                now=now(),
            )
            if updated:
                await record_audit_event(
                    session,
                    pending.actor,
                    action=f"invitation.email_{'sent' if status == 'sent' else status}",
                    resource_type="invitation",
                    resource_id=pending.invitation_id,
                    metadata={"attempts": attempts, "origin": pending.origin},
                )
            else:
                logger.info(
                    "invitation_email_result_ignored_stale",
                    invitation_id=str(pending.invitation_id),
                )
    except Exception:  # noqa: BLE001 - a background task must never raise into the server
        logger.exception("invitation_email_result_not_recorded", invitation_id=str(pending.invitation_id))
