"""Users router -- admin-triggered session revocation (API_DESIGN.md's
"logout everywhere" feature, admin half).

Owned by: app/api. `POST /auth/logout-all` (app/api/routers/auth.py) is the
self-service half -- a caller revoking their own sessions. This router is
the admin-triggered counterpart: an organization admin forcing a *different*
user's sessions to be revoked (e.g. offboarding, a suspected compromised
account).

"Organization admin" in this codebase is not a distinct role/flag -- see
`core.tenancy.service`'s module docstring: admin-ness is purely "holds the
`tenancy:manage` permission code (via whatever role grants it) within this
organization," the same permission every other tenancy-admin operation
(`configure_sso`, `create_project`, `create_access_rule`, `create_invitation`)
already requires. This router follows that exact same convention rather than
inventing a second admin concept.

No separate tenant-isolation check beyond `require_permission` is needed
before calling `revoke_all_sessions`: that function's own query is already
scoped to `(user_id, actor.organization_id)` (see its docstring in
`core.auth.service`), so an admin can only ever revoke sessions that exist
*within their own organization* -- a `user_id` belonging to a different
organization (or with no sessions in this one) simply revokes zero rows,
never leaks whether that user exists elsewhere.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.deps import CurrentIdentity, DbSession
from app.core.audit.service import record_audit_event
from app.core.auth import service as auth_service
from app.core.auth.schemas import LogoutAllResponse
from app.core.users.service import require_permission

router = APIRouter(prefix="/users", tags=["users"])

_MANAGE_PERMISSION = "tenancy:manage"


@router.post("/{user_id}/logout-all", response_model=LogoutAllResponse)
async def logout_all_sessions_for_user(
    user_id: uuid.UUID, actor: CurrentIdentity, session: DbSession
) -> LogoutAllResponse:
    """Revoke every session `user_id` holds within the caller's own
    organization. Only an organization admin (`tenancy:manage`) may call
    this -- see this module's docstring for what "admin" means here, and
    `LogoutAllResponse`'s own docstring for what "revoked" does and does not
    guarantee about a still-live access token.
    """
    require_permission(actor, _MANAGE_PERMISSION)

    revoked_count = await auth_service.revoke_all_sessions(session, user_id, actor.organization_id)
    await record_audit_event(
        session,
        actor,
        action="user.logout_all_sessions",
        resource_type="user",
        resource_id=user_id,
        metadata={"revoked_session_count": revoked_count, "revoked_by_admin": True},
    )
    return LogoutAllResponse(
        message="Successfully logged out from all sessions",
        revoked_session_count=revoked_count,
    )
