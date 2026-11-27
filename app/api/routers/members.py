"""Members router -- organization-scoped member offboarding (deactivate, reactivate, read status).

Owned by: app/api. A thin transport wrapper over `core.users.membership`, which enforces every rule
(`tenancy:manage` in the caller's OWN organization, target must be a current member with the identical 404
for strangers, no self-deactivation, last-administrator protection, mandatory reason, audit logging).

Deactivation is per ORGANIZATION: it never touches the global `users.is_active`, deletes no
organization-owned data, and ends every access token and refresh token the person holds for this
organization immediately. Reactivation issues no session: the person signs in again.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.deps import CurrentIdentity, DbSession
from app.core.users import membership
from app.core.users.schemas import MemberStatus, MemberStatusChange

router = APIRouter(tags=["members"])


@router.get("/organizations/{organization_id}/members/{user_id}/status", response_model=MemberStatus)
async def get_member_status(
    organization_id: uuid.UUID, user_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> MemberStatus:
    return await membership.get_member_status(session, actor, organization_id, user_id)


@router.post("/organizations/{organization_id}/members/{user_id}/deactivate", response_model=MemberStatus)
async def deactivate_member(
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    data: MemberStatusChange,
    actor: CurrentIdentity,
    session: DbSession,
) -> MemberStatus:
    result = await membership.deactivate_member(session, actor, organization_id, user_id, reason=data.reason)
    # Commit BEFORE answering (the request-scoped session otherwise commits after the response is sent), so
    # "200" always means the member is already locked out.
    await session.commit()
    return result


@router.post("/organizations/{organization_id}/members/{user_id}/reactivate", response_model=MemberStatus)
async def reactivate_member(
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    data: MemberStatusChange,
    actor: CurrentIdentity,
    session: DbSession,
) -> MemberStatus:
    result = await membership.reactivate_member(session, actor, organization_id, user_id, reason=data.reason)
    await session.commit()
    return result
