"""User-level security events: audit trail and session revocation.

A password is a USER-level credential, so the events around it are not tied to
one organization -- but `audit_logs` rows are organization-scoped (row-level
security hides NULL-organization rows from tenants and `ekip_app` could not
insert one anyway). So every event is recorded once PER organization the person
belongs to, with that organization's tenant context set, which is also exactly
where each organization's administrators look. A person with no organization
produces a log line only.

Never put a token, password, hash or email address in `metadata`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.service import record_audit_event
from app.core.auth import repository
from app.core.users import service as users_service
from app.database.session import set_tenant_context
from app.shared.config.logging import get_logger
from app.shared.schemas import ActorKind, Identity

logger = get_logger(__name__)


async def audit_user_event(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    action: str,
    metadata: dict | None = None,
) -> int:
    """Append `action` to the audit trail of every organization `user_id` belongs to.
    Returns how many organizations recorded it. Leaves the tenant context set to the
    last organization written -- callers that go on to tenant-scoped work set their own."""
    organization_ids = await users_service.list_organizations_for_login(session, user_id)
    for organization_id in organization_ids:
        await set_tenant_context(session, organization_id)
        actor = Identity(
            kind=ActorKind.USER,
            subject=str(user_id),
            organization_id=organization_id,
            user_id=user_id,
        )
        await record_audit_event(
            session,
            actor,
            action=action,
            resource_type="user",
            resource_id=user_id,
            metadata=metadata,
        )
    if not organization_ids:
        logger.info("security_event_no_organization", action=action, user_id=str(user_id))
    return len(organization_ids)


async def revoke_all_sessions_everywhere(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    revoked_at: datetime,
    audit_action: str,
) -> int:
    """End every session of `user_id` in every organization: all refresh-token
    families are revoked (the caller has already stamped `users.sessions_valid_after`,
    which kills already-issued access tokens), then the event is audited. Returns the
    number of refresh tokens revoked."""
    revoked = await repository.revoke_all_refresh_tokens_everywhere(
        session, user_id=user_id, revoked_at=revoked_at
    )
    await audit_user_event(
        session,
        user_id=user_id,
        action=audit_action,
        metadata={"refresh_tokens_revoked": revoked},
    )
    return revoked
