"""SCIM 2.0 protocol endpoints (`/scim/v2/*`) for Microsoft Entra ID and Okta.

Owned by: app/api. A thin transport over `core.scim.service`. This surface is deliberately separate from every
other authentication path:

* it is behind the `SCIM_ENABLED` kill switch (off by default) AND each organization's own switch;
* it authenticates ONLY an organization-specific opaque SCIM bearer token (`scim_...`); a REST or MCP access
  token is not accepted here, and a SCIM token is not accepted anywhere else (those verify signed JWTs);
* the organization is taken from the token, never from the URL, a header or a query parameter;
* every call is rate limited per token, refused for a suspended organization, and runs with that organization's
  tenant context so row-level security confines it even if a filter were wrong;
* errors are RFC 7644 error bodies (`application/scim+json`), never the application's own error shape.

Supported: ServiceProviderConfig, Schemas, ResourceTypes, Users (GET list with `userName eq` / `externalId eq`,
GET one, POST, PUT, PATCH, DELETE) and, since Phase 4, Groups. NOT supported (answered 501): Bulk, `/Me`, sorting,
ETags, other filters.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse

from app.api.deps import DbSession
from app.api.rate_limit import enforce_rate_limit
from app.core.exceptions import RateLimitedError
from app.core.scim import groups, repository, schemas, tokens
from app.core.scim import service as scim_service
from app.core.scim.errors import ScimError
from app.core.scim.service import ScimContext
from app.core.users import repository as users_repository
from app.database.session import set_tenant_context
from app.shared.config.settings import get_settings
from app.shared.organization_status import is_suspended

router = APIRouter(prefix="/scim/v2", tags=["scim"])

MEDIA_TYPE = "application/scim+json"
_MAX_BODY = 1_000_000


def scim_response(content: Any, status: int = 200) -> Response:
    return JSONResponse(content, status_code=status, media_type=MEDIA_TYPE)


async def scim_error_handler(request: Request, exc: ScimError) -> Response:
    return JSONResponse(exc.body(), status_code=exc.status, media_type=MEDIA_TYPE, headers=exc.headers)


async def get_scim_context(
    request: Request, session: DbSession, authorization: Annotated[str | None, Header()] = None
) -> ScimContext:
    settings = get_settings()
    if not settings.scim_enabled:
        raise ScimError(404, "SCIM provisioning is not enabled.", code="scim.disabled")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ScimError(
            401, "A bearer token is required.", code="scim.unauthorized",
            headers={"WWW-Authenticate": 'Bearer realm="EKIP SCIM"'},
        )
    auth = await tokens.authenticate(session, authorization.split(" ", 1)[1].strip())
    await set_tenant_context(session, auth.organization_id)
    config = await repository.get_config(session, auth.organization_id)
    if config is None or not config.enabled:
        raise ScimError(403, "SCIM provisioning is disabled for this organization.", code="scim.org_disabled")
    exists, status, _epoch = await users_repository.get_organization_access_state(session, auth.organization_id)
    if not exists or is_suspended(status):
        raise ScimError(403, "This organization is not available.", code="scim.org_suspended")
    try:
        await enforce_rate_limit(
            scope="scim.token", key=str(auth.token_id), requests_per_minute=float(settings.scim_requests_per_minute),
            error_code="rate_limited.scim_token",
        )
    except RateLimitedError as exc:
        raise ScimError(429, "Too many requests. Slow down and retry.", code="scim.rate_limited",
                        headers={"Retry-After": "30"}) from exc
    now = datetime.now(timezone.utc)
    prefix = (authorization.split(" ", 1)[1].strip())[:12]
    await repository.touch_token(session, auth.token_id, now=now)
    return ScimContext(
        organization_id=auth.organization_id,
        token_id=auth.token_id,
        token_prefix=prefix,
        config=config,
        base_url=str(request.base_url).rstrip("/") + "/scim/v2",
    )


Ctx = Annotated[ScimContext, Depends(get_scim_context)]


async def _run(session: DbSession, ctx: ScimContext, call: Callable[[], Awaitable[Any]]) -> Any:
    """Run one operation and record its outcome on the organization's SCIM health fields. A `ScimError` is a
    deliberate answer to the identity provider raised at a consistent point (including a held deactivation, whose
    pending record and audit event must survive), so it is COMMITTED before it is re-raised; any other
    exception rolls the whole request back."""
    now = datetime.now(timezone.utc)
    try:
        result = await call()
    except ScimError as exc:
        await repository.record_outcome(session, ctx.organization_id, now=now, error_code=exc.code)
        await session.commit()
        raise
    await repository.record_outcome(session, ctx.organization_id, now=now, error_code=None)
    await session.commit()
    return result


async def _json_body(request: Request) -> Any:
    raw = await request.body()
    if len(raw) > _MAX_BODY:
        raise ScimError(413, "The request body is too large.", code="scim.too_large")
    try:
        return json.loads(raw or b"null")
    except (ValueError, UnicodeDecodeError):
        raise ScimError(400, "The request body is not valid JSON.", "invalidSyntax", code="scim.invalid_syntax") from None


def _int_param(value: str | None, name: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        raise ScimError(400, f"'{name}' must be an integer.", "invalidValue", code="scim.invalid_value") from None


# ------------------------------------------------------------------------------------------------ discovery


@router.get("/ServiceProviderConfig")
async def service_provider_config(ctx: Ctx) -> Response:
    return scim_response(schemas.service_provider_config(ctx.base_url))


@router.get("/Schemas")
async def list_schemas(ctx: Ctx) -> Response:
    items = [schemas.user_schema(ctx.base_url), schemas.group_schema(ctx.base_url)]
    return scim_response(schemas.list_response(items, len(items), 1))


@router.get("/Schemas/{schema_id}")
async def get_schema(schema_id: str, ctx: Ctx) -> Response:
    for item in (schemas.user_schema(ctx.base_url), schemas.group_schema(ctx.base_url)):
        if item["id"] == schema_id:
            return scim_response(item)
    raise ScimError(404, "Schema not found.", code="scim.not_found")


@router.get("/ResourceTypes")
async def list_resource_types(ctx: Ctx) -> Response:
    items = schemas.resource_types(ctx.base_url, groups=True)
    return scim_response(schemas.list_response(items, len(items), 1))


@router.get("/ResourceTypes/{type_id}")
async def get_resource_type(type_id: str, ctx: Ctx) -> Response:
    for item in schemas.resource_types(ctx.base_url, groups=True):
        if item["id"].lower() == type_id.lower():
            return scim_response(item)
    raise ScimError(404, "Resource type not found.", code="scim.not_found")


# ------------------------------------------------------------------------------------------------ users


@router.get("/Users")
async def list_users(
    ctx: Ctx,
    session: DbSession,
    filter: str | None = None,  # noqa: A002 - the SCIM query parameter's name
    startIndex: str | None = None,  # noqa: N803
    count: str | None = None,
    sortBy: str | None = None,  # noqa: N803
) -> Response:
    if sortBy:
        raise ScimError(400, "Sorting is not supported.", "invalidValue", code="scim.unsupported")
    body = await _run(
        session, ctx,
        lambda: scim_service.list_users(
            session, ctx, filter_text=filter, start_index=_int_param(startIndex, "startIndex"),
            count=_int_param(count, "count"),
        ),
    )
    return scim_response(body)


@router.get("/Users/{identity_id}")
async def get_user(identity_id: str, ctx: Ctx, session: DbSession) -> Response:
    return scim_response(await _run(session, ctx, lambda: scim_service.get_user(session, ctx, identity_id)))


@router.post("/Users")
async def create_user(request: Request, ctx: Ctx, session: DbSession) -> Response:
    payload = await _json_body(request)
    body = await _run(session, ctx, lambda: scim_service.create_user(session, ctx, payload))
    return scim_response(body, 201)


@router.put("/Users/{identity_id}")
async def replace_user(identity_id: str, request: Request, ctx: Ctx, session: DbSession) -> Response:
    payload = await _json_body(request)
    return scim_response(await _run(session, ctx, lambda: scim_service.replace_user(session, ctx, identity_id, payload)))


@router.patch("/Users/{identity_id}")
async def patch_user(identity_id: str, request: Request, ctx: Ctx, session: DbSession) -> Response:
    payload = await _json_body(request)
    return scim_response(await _run(session, ctx, lambda: scim_service.patch_user(session, ctx, identity_id, payload)))


@router.delete("/Users/{identity_id}")
async def delete_user(identity_id: str, ctx: Ctx, session: DbSession) -> Response:
    await _run(session, ctx, lambda: scim_service.delete_user(session, ctx, identity_id))
    return Response(status_code=204)


# ------------------------------------------------------------------------------------------------ groups


@router.get("/Groups")
async def list_groups(
    ctx: Ctx,
    session: DbSession,
    filter: str | None = None,  # noqa: A002 - the SCIM query parameter's name
    startIndex: str | None = None,  # noqa: N803
    count: str | None = None,
    sortBy: str | None = None,  # noqa: N803
) -> Response:
    if sortBy:
        raise ScimError(400, "Sorting is not supported.", "invalidValue", code="scim.unsupported")
    body = await _run(
        session, ctx,
        lambda: groups.list_groups(
            session, ctx.organization_id, ctx.base_url, filter_text=filter,
            start_index=_int_param(startIndex, "startIndex"), count=_int_param(count, "count"),
        ),
    )
    return scim_response(body)


@router.get("/Groups/{group_id}")
async def get_group(group_id: str, ctx: Ctx, session: DbSession) -> Response:
    return scim_response(
        await _run(session, ctx, lambda: groups.get_group(session, ctx.organization_id, ctx.base_url, group_id))
    )


@router.post("/Groups")
async def create_group(request: Request, ctx: Ctx, session: DbSession) -> Response:
    payload = await _json_body(request)
    body = await _run(
        session, ctx, lambda: groups.create_group(session, ctx.organization_id, ctx.audit_actor, ctx.base_url, payload)
    )
    return scim_response(body, 201)


@router.put("/Groups/{group_id}")
async def replace_group(group_id: str, request: Request, ctx: Ctx, session: DbSession) -> Response:
    payload = await _json_body(request)
    return scim_response(
        await _run(
            session, ctx,
            lambda: groups.replace_group(session, ctx.organization_id, ctx.audit_actor, ctx.base_url, group_id, payload),
        )
    )


@router.patch("/Groups/{group_id}")
async def patch_group(group_id: str, request: Request, ctx: Ctx, session: DbSession) -> Response:
    payload = await _json_body(request)
    return scim_response(
        await _run(
            session, ctx,
            lambda: groups.patch_group(session, ctx.organization_id, ctx.audit_actor, ctx.base_url, group_id, payload),
        )
    )


@router.delete("/Groups/{group_id}")
async def delete_group(group_id: str, ctx: Ctx, session: DbSession) -> Response:
    await _run(session, ctx, lambda: groups.delete_group(session, ctx.organization_id, ctx.audit_actor, group_id))
    return Response(status_code=204)


# ------------------------------------------------------------------------------------------------ not supported


@router.api_route("/Bulk", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def bulk_unsupported(ctx: Ctx) -> Response:
    raise ScimError(501, "Bulk operations are not supported.", code="scim.unsupported")


@router.api_route("/Me", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def me_unsupported(ctx: Ctx) -> Response:
    raise ScimError(501, "The /Me endpoint is not supported.", code="scim.unsupported")
