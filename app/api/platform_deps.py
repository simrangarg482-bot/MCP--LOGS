"""FastAPI dependencies for the platform-operator surface (`/platform/*`).

Parallel to, and deliberately independent of, `app.api.deps` (tenant identity): a platform request never
resolves an `Identity`, never sets a tenant context and never reads tenant roles/permissions. The kill
switch is the first thing every dependency checks.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import structlog
from fastapi import Depends, Header

from app.api.deps import DbSession, _extract_bearer_token
from app.api.rate_limit import enforce_rate_limit
from app.core.exceptions import PermissionDeniedError
from app.core.platform import auth as platform_auth
from app.core.platform.schemas import PlatformOperator


async def require_platform_enabled() -> None:
    """Router-level guard: with the kill switch off every platform route behaves as if it did not exist."""
    platform_auth.assert_platform_enabled()


async def get_current_operator(
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> PlatformOperator:
    platform_auth.assert_platform_enabled()
    token = _extract_bearer_token(authorization)
    operator = await platform_auth.authenticate(session, token)
    structlog.contextvars.bind_contextvars(platform_operator_id=str(operator.user_id))
    return operator


CurrentOperator = Annotated[PlatformOperator, Depends(get_current_operator)]


async def get_admin_operator(operator: CurrentOperator) -> PlatformOperator:
    """Operators with the `admin` role only (state-changing actions). `viewer` may read."""
    if not operator.can_transition:
        raise PermissionDeniedError(
            "This action requires a platform administrator.", error_code="platform.admin_required"
        )
    return operator


AdminOperator = Annotated[PlatformOperator, Depends(get_admin_operator)]


def rate_limit_by_operator(
    *, scope: str, requests_per_minute: float
) -> Callable[[CurrentOperator], Coroutine[Any, Any, None]]:
    async def _dependency(operator: CurrentOperator) -> None:
        await enforce_rate_limit(
            scope=scope,
            key=f"operator:{operator.user_id}",
            requests_per_minute=requests_per_minute,
            error_code="rate_limited.operator",
        )

    return _dependency
