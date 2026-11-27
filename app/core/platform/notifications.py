"""Emailing an organization's administrators about a suspension / reactivation.

Two halves, like the invitation and password-reset mail: `prepare_status_notices` runs INSIDE the request
(it reads who the administrators are under the organization's tenant context) and
`deliver_status_notices` runs AFTER the response as a background task. Because delivery is a background
task, an email failure can never undo or fail the status change that triggered it.

Privacy: the renderers are handed only the organization name and the customer-facing message. The
operator's reason and private note are not parameters of anything in this module, so they cannot reach an
email. Nothing here logs an address or a message body.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.users import repository as users_repository
from app.database.models.core_models import User
from app.database.session import set_tenant_context
from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings
from app.shared.email import EmailDeliveryError, EmailNotConfiguredError, EmailSender, get_email_sender
from app.shared.email.templates import (
    render_organization_reactivated_email,
    render_organization_suspended_email,
)

logger = get_logger(__name__)

_DELIVERY_ATTEMPTS = 2


@dataclass(frozen=True)
class PendingStatusNotice:
    to: str
    kind: str  # "suspended" | "reactivated"
    organization_name: str
    customer_message: str | None
    organization_id: uuid.UUID

    def __repr__(self) -> str:  # never print the address
        return f"PendingStatusNotice(kind={self.kind!r}, organization_id={self.organization_id})"


async def prepare_status_notices(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    organization_name: str,
    new_status: str,
    previous_status: str,
    customer_message: str | None,
) -> list[PendingStatusNotice]:
    """Who to tell, decided from the database at this moment. Only suspension and reactivation notify."""
    if new_status == "suspended":
        kind = "suspended"
    elif new_status == "active" and previous_status == "suspended":
        kind = "reactivated"
    else:
        return []
    await set_tenant_context(session, organization_id)
    admin_ids = await users_repository.list_org_admin_user_ids(session, organization_id, lock=False)
    if not admin_ids:
        return []
    emails = (await session.execute(select(User.email).where(User.id.in_(admin_ids)))).scalars().all()
    return [
        PendingStatusNotice(
            to=email,
            kind=kind,
            organization_name=organization_name,
            customer_message=customer_message if kind == "suspended" else None,
            organization_id=organization_id,
        )
        for email in sorted(set(emails))
    ]


def _render(notice: PendingStatusNotice):
    if notice.kind == "suspended":
        return render_organization_suspended_email(
            to=notice.to,
            organization_name=notice.organization_name,
            customer_message=notice.customer_message,
        )
    base = get_settings().frontend_public_base_url.rstrip("/")
    return render_organization_reactivated_email(
        to=notice.to, organization_name=notice.organization_name, sign_in_url=f"{base}/login"
    )


async def deliver_status_notices(
    notices: list[PendingStatusNotice],
    *,
    sender: EmailSender | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Best-effort delivery; returns how many were accepted by the provider. Never raises: a provider
    failure is logged (without any address or content) and swallowed, because the status change it
    reports on has already been committed."""
    sent = 0
    for notice in notices:
        try:
            message = _render(notice)
            for attempt in range(1, _DELIVERY_ATTEMPTS + 1):
                try:
                    await (sender or get_email_sender()).send(message)
                    sent += 1
                    break
                except EmailNotConfiguredError:
                    logger.warning("organization_status_email_not_configured", kind=notice.kind)
                    break
                except EmailDeliveryError as exc:
                    logger.warning(
                        "organization_status_email_attempt_failed",
                        kind=notice.kind,
                        organization_id=str(notice.organization_id),
                        attempt=attempt,
                        error=str(exc),
                    )
                    if attempt < _DELIVERY_ATTEMPTS:
                        await sleep(2**attempt)
                    else:
                        logger.error(
                            "organization_status_email_failed",
                            kind=notice.kind,
                            organization_id=str(notice.organization_id),
                        )
        except Exception:  # noqa: BLE001 - nothing here may surface to the caller
            logger.exception("organization_status_email_error", kind=notice.kind)
    return sent
