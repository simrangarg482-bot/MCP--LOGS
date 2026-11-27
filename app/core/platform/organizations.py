"""Platform-operator view of, and control over, organizations.

Reads go through SECURITY DEFINER functions that verify the operator inside the database (the runtime role
cannot read the history or operator tables); every status change goes through the Phase 3 protected
function `platform_set_organization_status` via `core.tenancy.lifecycle`. This module adds the API-level
semantics on top: which transition each verb means, and the notification that follows.

Verbs vs. the database's four allowed transitions:
  activate    onboarding -> active
  suspend     onboarding | active -> suspended
  reactivate  suspended -> active
The database function accepts `active` from both `onboarding` and `suspended`, so "activate" and
"reactivate" are told apart here -- under a row lock taken in the SAME transaction (`SELECT ... FOR UPDATE`,
which the runtime role may take through its UPDATE privilege on `name`), so the status checked is the
status transitioned. Concurrent requests serialize on that lock; the loser sees the new status and gets a
clean conflict.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.platform import notifications
from app.core.platform.schemas import (
    PlatformOperator,
    PlatformOrganizationDetail,
    PlatformOrganizationList,
    PlatformOrganizationSummary,
    PlatformStatusHistory,
    PlatformStatusHistoryEntry,
    StatusChangeResult,
)
from app.core.tenancy import lifecycle

_VALID_STATUSES = {"onboarding", "active", "suspended"}


def _db_error(exc: DBAPIError) -> Exception:
    code = lifecycle._sqlstate(exc)  # noqa: SLF001 - same translation table as the transition wrapper
    if code == "EK003":
        return PermissionDeniedError("Platform operator access is required.", error_code="platform.operator_required")
    if code == "EK004":
        return NotFoundError("Organization not found.", error_code="organization.not_found")
    if code == "EK005":
        return ValidationError("Invalid request.", error_code="platform.invalid_request")
    return exc


async def list_organizations(
    session: AsyncSession,
    operator: PlatformOperator,
    *,
    search: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> PlatformOrganizationList:
    if status is not None and status not in _VALID_STATUSES:
        raise ValidationError("Unknown status filter.", error_code="platform.invalid_request")
    try:
        rows = (
            await session.execute(
                text(
                    "SELECT * FROM public.platform_list_organizations(:op, :search, :status, :limit, :offset)"
                ),
                {"op": operator.user_id, "search": search, "status": status, "limit": limit, "offset": offset},
            )
        ).all()
    except DBAPIError as exc:
        raise _db_error(exc) from exc
    return PlatformOrganizationList(
        items=[
            PlatformOrganizationSummary(
                id=r[0], name=r[1], slug=r[2], status=r[3], status_changed_at=r[4], created_at=r[5],
                member_count=r[6],
            )
            for r in rows
        ],
        total=int(rows[0][7]) if rows else 0,
        limit=limit,
        offset=offset,
    )


async def get_organization(
    session: AsyncSession, operator: PlatformOperator, organization_id: uuid.UUID
) -> PlatformOrganizationDetail:
    try:
        row = (
            await session.execute(
                text("SELECT * FROM public.platform_get_organization(:op, :org)"),
                {"op": operator.user_id, "org": organization_id},
            )
        ).first()
    except DBAPIError as exc:
        raise _db_error(exc) from exc
    if row is None:
        raise NotFoundError("Organization not found.", error_code="organization.not_found")
    return PlatformOrganizationDetail(
        id=row[0], name=row[1], slug=row[2], status=row[3], status_changed_at=row[4],
        status_message_public=row[5], created_at=row[6], member_count=row[7],
        session_epoch=row[8], history_count=row[9],
    )


async def status_history(
    session: AsyncSession,
    operator: PlatformOperator,
    organization_id: uuid.UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> PlatformStatusHistory:
    try:
        rows = (
            await session.execute(
                text("SELECT * FROM public.platform_organization_status_history(:op, :org, :limit, :offset)"),
                {"op": operator.user_id, "org": organization_id, "limit": limit, "offset": offset},
            )
        ).all()
    except DBAPIError as exc:
        raise _db_error(exc) from exc
    return PlatformStatusHistory(
        items=[
            PlatformStatusHistoryEntry(
                id=r[0], previous_status=r[1], new_status=r[2], reason=r[3], customer_message=r[4],
                internal_note=r[5], operator_user_id=r[6], operator_email=r[7], changed_at=r[8], request_id=r[9],
            )
            for r in rows
        ],
        total=int(rows[0][10]) if rows else 0,
        limit=limit,
        offset=offset,
    )


async def _lock_status(session: AsyncSession, organization_id: uuid.UUID) -> str:
    row = (
        await session.execute(
            text("SELECT status FROM public.organizations WHERE id = :id FOR UPDATE"), {"id": organization_id}
        )
    ).first()
    if row is None:
        raise NotFoundError("Organization not found.", error_code="organization.not_found")
    return row[0]


async def _transition(
    session: AsyncSession,
    operator: PlatformOperator,
    organization_id: uuid.UUID,
    *,
    verb: str,
    allowed_from: set[str],
    new_status: str,
    reason: str,
    customer_message: str | None = None,
    internal_note: str | None = None,
    request_id: str | None = None,
) -> tuple[StatusChangeResult, list[notifications.PendingStatusNotice]]:
    if not operator.can_transition:
        raise PermissionDeniedError(
            "This action requires a platform administrator.", error_code="platform.admin_required"
        )
    current = await _lock_status(session, organization_id)
    if current not in allowed_from:
        if current == new_status:
            raise ConflictError(
                f"The organization is already {current}.", error_code="organization.already_in_status"
            )
        hint = {
            "activate": "Only an onboarding organization can be activated (use reactivate for a suspended one).",
            "reactivate": "Only a suspended organization can be reactivated.",
            "suspend": "Only an onboarding or active organization can be suspended.",
        }[verb]
        raise ConflictError(hint, error_code="organization.invalid_transition")
    change = await lifecycle.set_organization_status(
        session,
        operator_user_id=operator.user_id,
        organization_id=organization_id,
        new_status=new_status,
        reason=reason,
        customer_message=customer_message,
        internal_note=internal_note,
        request_id=request_id,
    )
    detail = await get_organization(session, operator, organization_id)
    # Deciding who to notify must never endanger the committed transition: it runs in a SAVEPOINT, and
    # any failure only means nobody is emailed (logged), never a rolled-back status change.
    notices: list[notifications.PendingStatusNotice] = []
    try:
        async with session.begin_nested():
            notices = await notifications.prepare_status_notices(
                session,
                organization_id=organization_id,
                organization_name=detail.name,
                new_status=change.new_status,
                previous_status=change.previous_status,
                customer_message=customer_message,
            )
    except Exception:  # noqa: BLE001
        notifications.logger.exception(
            "organization_status_notice_preparation_failed", organization_id=str(organization_id)
        )
        notices = []
    result = StatusChangeResult(
        organization=detail,
        previous_status=change.previous_status,  # type: ignore[arg-type]
        new_status=change.new_status,  # type: ignore[arg-type]
        history_id=change.history_id,
        notification_queued=bool(notices),
    )
    return result, notices


async def activate(session, operator, organization_id, *, reason, request_id=None):
    return await _transition(
        session, operator, organization_id, verb="activate", allowed_from={"onboarding"},
        new_status="active", reason=reason, request_id=request_id,
    )


async def suspend(
    session, operator, organization_id, *, reason, customer_message=None, internal_note=None, request_id=None
):
    return await _transition(
        session, operator, organization_id, verb="suspend", allowed_from={"onboarding", "active"},
        new_status="suspended", reason=reason, customer_message=customer_message,
        internal_note=internal_note, request_id=request_id,
    )


async def reactivate(session, operator, organization_id, *, reason, request_id=None):
    return await _transition(
        session, operator, organization_id, verb="reactivate", allowed_from={"suspended"},
        new_status="active", reason=reason, request_id=request_id,
    )
