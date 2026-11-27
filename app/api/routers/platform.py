"""Platform-operator API (`/platform/*`).

Everything here is behind the `PLATFORM_ADMIN_ENABLED` kill switch (off by default, refused in
production) and a SEPARATE platform token (audience `ekip-platform`); tenant tokens do not work here and
platform tokens do not work on tenant routes. Tenant roles are never consulted.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel

from app.api.deps import DbSession
from app.api.platform_deps import AdminOperator, CurrentOperator, rate_limit_by_operator
from app.api.rate_limit import enforce_rate_limit, rate_limit_by_ip
from app.core.platform import auth as platform_auth
from app.core.storage import service as storage_service
from app.core.storage.service import PlatformStorage
from app.core.platform import notifications as platform_notifications
from app.core.platform import organizations as platform_orgs
from app.core.platform.schemas import (
    ActivateRequest,
    PlatformLoginRequest,
    PlatformOperator,
    PlatformOrganizationDetail,
    PlatformOrganizationList,
    PlatformSession,
    PlatformStatusHistory,
    ReactivateRequest,
    StatusChangeResult,
    SuspendRequest,
)

router = APIRouter(prefix="/platform", tags=["platform"])

_LOGIN_IP_LIMIT = rate_limit_by_ip(scope="platform.login", requests_per_minute=5)
_OPERATOR_RATE = rate_limit_by_operator(scope="platform.request", requests_per_minute=120)


class PlatformStatus(BaseModel):
    available: bool


@router.get("/status", response_model=PlatformStatus)
async def platform_status() -> PlatformStatus:
    """Whether the platform surface is switched on. Public and constant-cost: the console uses it to
    show an "unavailable" state instead of a login form that cannot work."""
    return PlatformStatus(available=platform_auth.platform_access_available())


@router.post("/auth/login", response_model=PlatformSession, dependencies=[Depends(_LOGIN_IP_LIMIT)])
async def platform_login(data: PlatformLoginRequest, session: DbSession) -> PlatformSession:
    platform_auth.assert_platform_enabled()
    # A second budget keyed by the (hashed) login name, so a distributed guessing run against one
    # account is throttled even when it comes from many addresses.
    name_key = hashlib.sha256(data.email.strip().lower().encode()).hexdigest()[:32]
    await enforce_rate_limit(
        scope="platform.login_account",
        key=name_key,
        requests_per_minute=5,
        error_code="rate_limited.account",
    )
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    return await platform_auth.login(session, data, request_id=request_id)


@router.get("/auth/me", response_model=PlatformOperator, dependencies=[Depends(_OPERATOR_RATE)])
async def platform_me(operator: CurrentOperator) -> PlatformOperator:
    return operator


# --- organization lifecycle (Phase 5) ------------------------------------------------------------------------

_TRANSITION_RATE = rate_limit_by_operator(scope="platform.transition", requests_per_minute=30)


@router.get("/storage", response_model=PlatformStorage, dependencies=[Depends(_OPERATOR_RATE)])
async def platform_storage(operator: CurrentOperator, session: DbSession) -> PlatformStorage:
    """Database storage for platform operators: MEASURED totals and per-table sizes, configured limit and the
    warning state, measured growth between snapshots, and per-organization ESTIMATES (clearly flagged). Read
    from the latest daily snapshot -- no expensive query runs per request."""
    return await storage_service.get_platform_storage(session, operator.user_id)


def _request_id() -> str | None:
    return structlog.contextvars.get_contextvars().get("request_id")


@router.get(
    "/organizations", response_model=PlatformOrganizationList, dependencies=[Depends(_OPERATOR_RATE)]
)
async def list_organizations(
    operator: CurrentOperator,
    session: DbSession,
    search: Annotated[str | None, Query(max_length=200)] = None,
    status: Annotated[str | None, Query(pattern="^(onboarding|active|suspended)$")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PlatformOrganizationList:
    return await platform_orgs.list_organizations(
        session, operator, search=search, status=status, limit=limit, offset=offset
    )


@router.get(
    "/organizations/{organization_id}",
    response_model=PlatformOrganizationDetail,
    dependencies=[Depends(_OPERATOR_RATE)],
)
async def get_organization(
    organization_id: uuid.UUID, operator: CurrentOperator, session: DbSession
) -> PlatformOrganizationDetail:
    return await platform_orgs.get_organization(session, operator, organization_id)


@router.get(
    "/organizations/{organization_id}/status-history",
    response_model=PlatformStatusHistory,
    dependencies=[Depends(_OPERATOR_RATE)],
)
async def organization_status_history(
    organization_id: uuid.UUID,
    operator: CurrentOperator,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PlatformStatusHistory:
    return await platform_orgs.status_history(session, operator, organization_id, limit=limit, offset=offset)


@router.post(
    "/organizations/{organization_id}/activate",
    response_model=StatusChangeResult,
    dependencies=[Depends(_TRANSITION_RATE)],
)
async def activate_organization(
    organization_id: uuid.UUID,
    data: ActivateRequest,
    operator: AdminOperator,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> StatusChangeResult:
    result, notices = await platform_orgs.activate(
        session, operator, organization_id, reason=data.reason, request_id=_request_id()
    )
    # Commit BEFORE answering. The request-scoped session otherwise commits only after the response has been
    # sent, so a client that reacts to "200" by immediately reading (the console reloading the history, a
    # tenant admin hitting the API) could still see the old state. Emails are queued after this, so they
    # never describe a change that is not durable yet.
    await session.commit()
    if notices:
        background_tasks.add_task(platform_notifications.deliver_status_notices, notices)
    return result


@router.post(
    "/organizations/{organization_id}/suspend",
    response_model=StatusChangeResult,
    dependencies=[Depends(_TRANSITION_RATE)],
)
async def suspend_organization(
    organization_id: uuid.UUID,
    data: SuspendRequest,
    operator: AdminOperator,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> StatusChangeResult:
    result, notices = await platform_orgs.suspend(
        session,
        operator,
        organization_id,
        reason=data.reason,
        customer_message=data.customer_message,
        internal_note=data.internal_note,
        request_id=_request_id(),
    )
    # Commit BEFORE answering. The request-scoped session otherwise commits only after the response has been
    # sent, so a client that reacts to "200" by immediately reading (the console reloading the history, a
    # tenant admin hitting the API) could still see the old state. Emails are queued after this, so they
    # never describe a change that is not durable yet.
    await session.commit()
    if notices:
        background_tasks.add_task(platform_notifications.deliver_status_notices, notices)
    return result


@router.post(
    "/organizations/{organization_id}/reactivate",
    response_model=StatusChangeResult,
    dependencies=[Depends(_TRANSITION_RATE)],
)
async def reactivate_organization(
    organization_id: uuid.UUID,
    data: ReactivateRequest,
    operator: AdminOperator,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> StatusChangeResult:
    result, notices = await platform_orgs.reactivate(
        session, operator, organization_id, reason=data.reason, request_id=_request_id()
    )
    # Commit BEFORE answering. The request-scoped session otherwise commits only after the response has been
    # sent, so a client that reacts to "200" by immediately reading (the console reloading the history, a
    # tenant admin hitting the API) could still see the old state. Emails are queued after this, so they
    # never describe a change that is not durable yet.
    await session.commit()
    if notices:
        background_tasks.add_task(platform_notifications.deliver_status_notices, notices)
    return result
