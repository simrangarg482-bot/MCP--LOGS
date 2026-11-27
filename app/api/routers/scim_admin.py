"""Tenant-administrator API for SCIM provisioning (Settings -> Provisioning).

Owned by: app/api. A thin wrapper over `core.scim.admin`, which enforces `tenancy:manage` in the caller's own
organization. These are ordinary tenant endpoints (authenticated with a normal access token); they are NOT the
SCIM protocol endpoints (`/scim/v2/*`, authenticated with a SCIM token) and NOT platform-operator controls.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.deps import CurrentIdentity, DbSession
from app.core.scim import admin, groups

router = APIRouter(tags=["scim-admin"])


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.get("/organizations/{organization_id}/scim", response_model=admin.ScimOverview)
async def get_scim_overview(
    organization_id: uuid.UUID, request: Request, actor: CurrentIdentity, session: DbSession
) -> admin.ScimOverview:
    return await admin.get_overview(session, actor, organization_id, base_url=_base(request))


@router.put("/organizations/{organization_id}/scim", response_model=admin.ScimOverview)
async def update_scim_config(
    organization_id: uuid.UUID, data: admin.ScimConfigUpdate, request: Request, actor: CurrentIdentity, session: DbSession
) -> admin.ScimOverview:
    return await admin.update_config(session, actor, organization_id, data, base_url=_base(request))


@router.get("/organizations/{organization_id}/scim/tokens", response_model=list[admin.ScimTokenInfo])
async def list_scim_tokens(
    organization_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> list[admin.ScimTokenInfo]:
    return await admin.list_tokens(session, actor, organization_id)


@router.post("/organizations/{organization_id}/scim/tokens", response_model=admin.ScimTokenCreated, status_code=201)
async def create_scim_token(
    organization_id: uuid.UUID, data: admin.ScimTokenCreate, actor: CurrentIdentity, session: DbSession
) -> admin.ScimTokenCreated:
    result = await admin.create_token(session, actor, organization_id, data)
    await session.commit()  # durable before the one-time secret is handed out
    return result


@router.post("/organizations/{organization_id}/scim/tokens/{token_id}/rotate", response_model=admin.ScimTokenCreated, status_code=201)
async def rotate_scim_token(
    organization_id: uuid.UUID, token_id: uuid.UUID, data: admin.ScimTokenRotate, actor: CurrentIdentity, session: DbSession
) -> admin.ScimTokenCreated:
    result = await admin.rotate_token(session, actor, organization_id, token_id, data)
    await session.commit()
    return result


@router.post("/organizations/{organization_id}/scim/tokens/{token_id}/revoke", response_model=admin.ScimTokenInfo)
async def revoke_scim_token(
    organization_id: uuid.UUID, token_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> admin.ScimTokenInfo:
    result = await admin.revoke_token(session, actor, organization_id, token_id)
    await session.commit()
    return result


@router.get("/organizations/{organization_id}/scim/identities", response_model=admin.ScimIdentityPage)
async def list_scim_identities(
    organization_id: uuid.UUID,
    actor: CurrentIdentity,
    session: DbSession,
    status: Annotated[str | None, Query(pattern="^(pending|linked)$")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> admin.ScimIdentityPage:
    return await admin.list_identities(session, actor, organization_id, status=status, offset=offset, limit=limit)


@router.post("/organizations/{organization_id}/scim/identities/{identity_id}/link", response_model=admin.ScimIdentityInfo)
async def link_scim_identity(
    organization_id: uuid.UUID, identity_id: uuid.UUID, data: admin.ScimIdentityLink, actor: CurrentIdentity, session: DbSession
) -> admin.ScimIdentityInfo:
    result = await admin.link_identity(session, actor, organization_id, identity_id, data)
    await session.commit()
    return result


@router.get("/organizations/{organization_id}/scim/pending", response_model=list[admin.PendingActionInfo])
async def list_scim_pending(
    organization_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> list[admin.PendingActionInfo]:
    return await admin.list_pending(session, actor, organization_id)


@router.post("/organizations/{organization_id}/scim/pending/{pending_id}/approve", response_model=admin.PendingActionInfo)
async def approve_scim_pending(
    organization_id: uuid.UUID, pending_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> admin.PendingActionInfo:
    result = await admin.decide_pending(session, actor, organization_id, pending_id, approve=True)
    await session.commit()
    return result


@router.post("/organizations/{organization_id}/scim/pending/{pending_id}/reject", response_model=admin.PendingActionInfo)
async def reject_scim_pending(
    organization_id: uuid.UUID, pending_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> admin.PendingActionInfo:
    result = await admin.decide_pending(session, actor, organization_id, pending_id, approve=False)
    await session.commit()
    return result


@router.get("/organizations/{organization_id}/scim/groups", response_model=list[groups.ScimGroupInfo])
async def list_scim_groups(
    organization_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> list[groups.ScimGroupInfo]:
    return await groups.list_groups_admin(session, actor, organization_id)


@router.put("/organizations/{organization_id}/scim/groups/{group_id}/roles", response_model=groups.ScimGroupInfo)
async def set_scim_group_roles(
    organization_id: uuid.UUID,
    group_id: uuid.UUID,
    data: groups.ScimGroupRolesUpdate,
    actor: CurrentIdentity,
    session: DbSession,
) -> groups.ScimGroupInfo:
    result = await groups.set_group_roles(session, actor, organization_id, group_id, data)
    await session.commit()
    return result
