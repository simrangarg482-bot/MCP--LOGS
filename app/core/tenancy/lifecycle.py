"""Thin, un-routed wrapper around the database's protected organization-status function.

Phase 3 only provides the database model; no API, CLI or UI calls this yet (Phases 4-5). It exists so
the one approved way to change a status is a single, tested call, and so database error codes become
the application's exceptions in one place.

The SQL function `platform_set_organization_status` does the authorization (active platform operator),
locking, transition validation, status/epoch/refresh-token changes, history and audit writes in one
transaction. This module adds none of that logic -- it only calls and translates.
"""
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError, ValidationError


@dataclass(frozen=True)
class StatusChange:
    previous_status: str
    new_status: str
    history_id: uuid.UUID
    session_epoch: int


_CODE_MESSAGES = {
    "EK001": "That organization status change is not allowed.",
    "EK002": "The organization is already in that status.",
    "EK005": "Invalid organization status request (reason is required; check field lengths).",
}

_SQLSTATE_ERRORS = {
    "EK001": lambda msg: ConflictError(msg, error_code="organization.invalid_transition"),
    "EK002": lambda msg: ConflictError(msg, error_code="organization.already_in_status"),
    "EK003": lambda msg: PermissionDeniedError(
        "Platform operator access is required.", error_code="platform.operator_required"
    ),
    "EK004": lambda msg: NotFoundError("Organization not found.", error_code="organization.not_found"),
    "EK005": lambda msg: ValidationError(msg, error_code="organization.invalid_status_request"),
}


def _sqlstate(exc: DBAPIError) -> str | None:
    original = getattr(exc, "orig", None)
    code = getattr(original, "sqlstate", None) or getattr(getattr(original, "__cause__", None), "sqlstate", None)
    return code if isinstance(code, str) else None


async def set_organization_status(
    session: AsyncSession,
    *,
    operator_user_id: uuid.UUID,
    organization_id: uuid.UUID,
    new_status: str,
    reason: str,
    customer_message: str | None = None,
    internal_note: str | None = None,
    request_id: str | None = None,
) -> StatusChange:
    """Call `platform_set_organization_status`. The caller owns the transaction (the function's
    effects commit or roll back with it)."""
    try:
        result = await session.execute(
            text(
                "SELECT * FROM public.platform_set_organization_status("
                ":operator, :org, :status, :reason, :message, :note, :request_id)"
            ),
            {
                "operator": operator_user_id,
                "org": organization_id,
                "status": new_status,
                "reason": reason,
                "message": customer_message,
                "note": internal_note,
                "request_id": request_id,
            },
        )
    except DBAPIError as exc:
        code = _sqlstate(exc)
        factory = _SQLSTATE_ERRORS.get(code or "")
        if factory is None:
            raise
        raise factory(_CODE_MESSAGES.get(code or "", "Invalid organization status request.")) from exc
    row = result.one()
    return StatusChange(
        previous_status=row[0], new_status=row[1], history_id=row[2], session_epoch=row[3]
    )


async def is_active_platform_operator(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """Ask the database whether `user_id` is an active platform operator (the application role has
    no direct access to `platform_operators`)."""
    result = await session.execute(
        text("SELECT public.platform_is_active_operator(:user_id)"), {"user_id": user_id}
    )
    return bool(result.scalar_one())
