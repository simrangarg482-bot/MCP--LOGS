"""Roles router -- organization-scoped custom roles and member role assignment.

Owned by: app/api. A thin transport wrapper over `core.users.role_service`,
which enforces every rule (`tenancy:manage`, no privilege escalation, built-in
roles immutable, last-administrator protection, in-use protection, audit
logging); nothing here re-implements an authorization decision.

Paths follow the `/organizations/{organization_id}/...` convention of the
tenancy admin routes. The `{organization_id}` must equal the caller's own
organization or the service refuses with `users.cross_organization_denied`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response

from app.api.deps import CurrentIdentity, DbSession
from app.core.users import role_service
from app.core.users.role_schemas import (
    MemberRoleAdd,
    MemberRoles,
    MemberRolesSet,
    PermissionInfo,
    Role,
    RoleCreate,
    RoleUpdate,
)

router = APIRouter(tags=["roles"])


@router.get("/organizations/{organization_id}/permissions", response_model=list[PermissionInfo])
async def list_permissions(organization_id: uuid.UUID, actor: CurrentIdentity) -> list[PermissionInfo]:
    """The assignable permission catalog (internal permissions are never listed)."""
    return role_service.list_permission_catalog(actor, organization_id)


@router.get("/organizations/{organization_id}/roles", response_model=list[Role])
async def list_roles(
    organization_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> list[Role]:
    return await role_service.list_roles(session, actor, organization_id)


@router.post("/organizations/{organization_id}/roles", response_model=Role, status_code=201)
async def create_role(
    organization_id: uuid.UUID, data: RoleCreate, actor: CurrentIdentity, session: DbSession
) -> Role:
    return await role_service.create_role(session, actor, organization_id, data)


@router.get("/organizations/{organization_id}/roles/{role_id}", response_model=Role)
async def get_role(
    organization_id: uuid.UUID, role_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> Role:
    return await role_service.get_role(session, actor, organization_id, role_id)


@router.patch("/organizations/{organization_id}/roles/{role_id}", response_model=Role)
async def update_role(
    organization_id: uuid.UUID,
    role_id: uuid.UUID,
    data: RoleUpdate,
    actor: CurrentIdentity,
    session: DbSession,
) -> Role:
    """Rename a custom role and/or replace its permission set."""
    return await role_service.update_role(session, actor, organization_id, role_id, data)


@router.delete("/organizations/{organization_id}/roles/{role_id}", status_code=204)
async def delete_role(
    organization_id: uuid.UUID, role_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> Response:
    await role_service.delete_role(session, actor, organization_id, role_id)
    return Response(status_code=204)


@router.put("/organizations/{organization_id}/members/{user_id}/roles", response_model=MemberRoles)
async def set_member_roles(
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    data: MemberRolesSet,
    actor: CurrentIdentity,
    session: DbSession,
) -> MemberRoles:
    """Replace the member's whole role set (at least one role)."""
    return await role_service.set_member_roles(session, actor, organization_id, user_id, data.role_ids)


@router.post(
    "/organizations/{organization_id}/members/{user_id}/roles",
    response_model=MemberRoles,
    status_code=201,
)
async def add_member_role(
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    data: MemberRoleAdd,
    actor: CurrentIdentity,
    session: DbSession,
) -> MemberRoles:
    return await role_service.add_member_role(session, actor, organization_id, user_id, data.role_id)


@router.delete(
    "/organizations/{organization_id}/members/{user_id}/roles/{role_id}",
    response_model=MemberRoles,
)
async def remove_member_role(
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    actor: CurrentIdentity,
    session: DbSession,
) -> MemberRoles:
    return await role_service.remove_member_role(session, actor, organization_id, user_id, role_id)
