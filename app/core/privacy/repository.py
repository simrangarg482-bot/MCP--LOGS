"""Persistence for core/privacy -- the counting queries that build a
deletion plan, and the mutations that execute it.

Owned by: core/privacy. Pure data access: every function issues one
statement and returns a count; all ownership reasoning and sequencing lives
in service.py.

**Every mutation here is scoped by BOTH `user_id` AND `organization_id`.**
This is the tenant-isolation invariant of the whole module, not an
optimization: an admin of organization A calling deletion for a user who
also belongs to organization B must affect only A's half of that user's
data. `project_memberships` has no `organization_id` column of its own, so
it is scoped through a `projects` subquery rather than being left unscoped
-- see `delete_project_memberships`.

**Every mutation is idempotent by construction.** They are all
`DELETE ... WHERE`/`UPDATE ... WHERE` statements: re-running one after it
has already succeeded matches zero rows and returns 0, which is a
successful no-op rather than an error. Nothing here uses "fetch, then
assume the row exists" -- that pattern is what makes deletion workflows
fail on retry.

Why `rowcount` and not `RETURNING`: these statements can match thousands of
rows (`agent_executions` for a heavy user), and the caller only needs the
count for its result record, never the row contents. `rowcount` is exactly
that, without materializing anything.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.agent_models import AgentExecution
from app.database.models.auth_models import RefreshToken
from app.database.models.core_models import Incident, User, UserRole
from app.database.models.tenancy_models import (
    ExternalIdentityMapping,
    Invitation,
    Project,
    ProjectMembership,
)


def _org_project_ids(organization_id: uuid.UUID):
    """Subquery: every project id belonging to `organization_id`.

    `project_memberships` carries only `(user_id, project_id, role_id)` --
    no `organization_id` -- so this is the only way to scope a membership
    operation to one tenant. Written as a subquery rather than two round
    trips so the scoping cannot be accidentally omitted by a caller.
    """
    return select(Project.id).where(Project.organization_id == organization_id).scalar_subquery()


# --------------------------------------------------------------------------
# discovery -- counting only, never mutating
# --------------------------------------------------------------------------


async def count_refresh_tokens(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> int:
    """Every refresh token row for this user in this org -- including
    already-revoked and already-expired ones. Revocation sets `revoked_at`
    but leaves the row (and its `user_id`) in place, so a revoked token is
    still user-attributable data that deletion is responsible for."""
    stmt = select(func.count()).select_from(RefreshToken).where(
        RefreshToken.user_id == user_id, RefreshToken.organization_id == organization_id
    )
    return int((await session.execute(stmt)).scalar_one())


async def count_user_roles(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> int:
    stmt = select(func.count()).select_from(UserRole).where(
        UserRole.user_id == user_id, UserRole.organization_id == organization_id
    )
    return int((await session.execute(stmt)).scalar_one())


async def count_project_memberships(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> int:
    stmt = select(func.count()).select_from(ProjectMembership).where(
        ProjectMembership.user_id == user_id,
        ProjectMembership.project_id.in_(_org_project_ids(organization_id)),
    )
    return int((await session.execute(stmt)).scalar_one())


async def count_external_identity_mappings(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> int:
    stmt = select(func.count()).select_from(ExternalIdentityMapping).where(
        ExternalIdentityMapping.user_id == user_id,
        ExternalIdentityMapping.organization_id == organization_id,
    )
    return int((await session.execute(stmt)).scalar_one())


async def count_agent_executions(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> int:
    stmt = select(func.count()).select_from(AgentExecution).where(
        AgentExecution.user_id == user_id, AgentExecution.organization_id == organization_id
    )
    return int((await session.execute(stmt)).scalar_one())


async def count_invitations_for_email(
    session: AsyncSession, email: str, organization_id: uuid.UUID
) -> int:
    """Invitations addressed TO this email in this org.

    Keyed by email, not by user id: an invitation records the address it was
    sent to, and may predate the invitee ever having a `users` row at all.
    This is the only table besides `users` that stores a raw email address
    (verified against the full schema), which is why it needs explicit
    handling rather than being covered by anonymizing the user row."""
    stmt = select(func.count()).select_from(Invitation).where(
        Invitation.email == email, Invitation.organization_id == organization_id
    )
    return int((await session.execute(stmt)).scalar_one())


async def count_incidents_reported(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> int:
    """Incidents this user reported. Counted for the plan's RETAIN section
    only -- never mutated. `incidents.reported_by` is `ON DELETE RESTRICT`
    (see `core_models.Incident`), which is the schema stating outright that
    incident history outlives the individual who filed it."""
    stmt = select(func.count()).select_from(Incident).where(
        Incident.reported_by == user_id, Incident.organization_id == organization_id
    )
    return int((await session.execute(stmt)).scalar_one())


async def get_user_email(session: AsyncSession, user_id: uuid.UUID) -> str | None:
    """The user's current email, or `None` if the user row is absent or has
    already been anonymized. Needed to find `invitations` rows, which are
    keyed by email rather than user id. Returning `None` for an
    already-anonymized user is what makes the invitation step skip cleanly
    on a second run instead of matching the placeholder address."""
    row = await session.get(User, user_id)
    if row is None or row.email is None:
        return None
    if is_anonymized_email(row.email):
        return None
    return row.email


async def user_exists(session: AsyncSession, user_id: uuid.UUID) -> bool:
    return (await session.get(User, user_id)) is not None


async def is_user_anonymized(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """Whether this user row has already been through anonymization -- the
    idempotency check the service uses to detect a repeat run."""
    row = await session.get(User, user_id)
    if row is None:
        return False
    return is_anonymized_email(row.email)


# --------------------------------------------------------------------------
# anonymization helpers
# --------------------------------------------------------------------------

#: RFC 2606 reserves `.invalid` as a TLD guaranteed never to resolve, so a
#: placeholder built on it can never collide with (or be mistaken for) a
#: real address, and can never accidentally receive mail.
_ANONYMIZED_EMAIL_DOMAIN = "deleted.invalid"
_ANONYMIZED_EMAIL_PREFIX = "deleted-user-"
ANONYMIZED_DISPLAY_NAME = "Deleted User"


def anonymized_email_for(user_id: uuid.UUID) -> str:
    """The placeholder address for a deleted user.

    `users.email` is `UNIQUE NOT NULL`, so it cannot simply be nulled --
    every anonymized row still needs a distinct value. Deriving it from the
    (surrogate, non-personal) user id satisfies uniqueness deterministically,
    which also makes anonymization idempotent: running it twice produces the
    identical value rather than a second distinct placeholder.
    """
    return f"{_ANONYMIZED_EMAIL_PREFIX}{user_id}@{_ANONYMIZED_EMAIL_DOMAIN}"


def is_anonymized_email(email: str | None) -> bool:
    return bool(email) and email.endswith(f"@{_ANONYMIZED_EMAIL_DOMAIN}")


# --------------------------------------------------------------------------
# execution -- mutations
# --------------------------------------------------------------------------


async def delete_refresh_tokens(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> int:
    stmt = sql_delete(RefreshToken).where(
        RefreshToken.user_id == user_id, RefreshToken.organization_id == organization_id
    )
    return int((await session.execute(stmt)).rowcount or 0)


async def delete_user_roles(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> int:
    stmt = sql_delete(UserRole).where(
        UserRole.user_id == user_id, UserRole.organization_id == organization_id
    )
    return int((await session.execute(stmt)).rowcount or 0)


async def delete_project_memberships(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> int:
    stmt = sql_delete(ProjectMembership).where(
        ProjectMembership.user_id == user_id,
        ProjectMembership.project_id.in_(_org_project_ids(organization_id)),
    )
    return int((await session.execute(stmt)).rowcount or 0)


async def delete_external_identity_mappings(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> int:
    stmt = sql_delete(ExternalIdentityMapping).where(
        ExternalIdentityMapping.user_id == user_id,
        ExternalIdentityMapping.organization_id == organization_id,
    )
    return int((await session.execute(stmt)).rowcount or 0)


async def anonymize_agent_executions(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> int:
    """Detach this user from their agent-execution history by nulling
    `user_id`, keeping the row.

    This is exactly what the column's own FK already declares
    (`ondelete="SET NULL"`, `agent_models.py`): the execution record is
    organization-level telemetry -- token counts, cost, confidence, which
    feed `GET /observability/agents` and the Knowledge Gap Agent's
    clustering -- whose value does not depend on who asked. Deleting the
    rows instead would silently rewrite the organization's own usage and
    cost history, which is not the user's data to erase.

    `input_summary` is deliberately left as-is: it is a structured summary,
    explicitly not the raw prompt (see `AgentExecution`'s docstring), and it
    is not user-attributable once `user_id` is null.
    """
    stmt = (
        update(AgentExecution)
        .where(
            AgentExecution.user_id == user_id,
            AgentExecution.organization_id == organization_id,
        )
        .values(user_id=None)
    )
    return int((await session.execute(stmt)).rowcount or 0)


async def anonymize_invitations_for_email(
    session: AsyncSession, email: str, organization_id: uuid.UUID, placeholder_email: str
) -> int:
    """Replace the raw invitee address on this org's invitations with
    `placeholder_email`, keeping the row.

    The row survives because `invitations` is partly an audit record of who
    invited whom (`invited_by` is `ON DELETE RESTRICT` for exactly that
    stated reason), and because deleting it would silently free the
    partial-unique "one pending invitation per email per organization"
    index in a way that changes org-level provisioning state. Only the
    address -- the actual personal datum -- is removed.
    """
    stmt = (
        update(Invitation)
        .where(Invitation.email == email, Invitation.organization_id == organization_id)
        .values(email=placeholder_email)
    )
    return int((await session.execute(stmt)).rowcount or 0)


async def anonymize_user_record(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Strip the personal fields from the `users` row and deactivate it.

    **This is the load-bearing step of the whole workflow.** The `users` row
    is one of only two places in the entire schema that stores raw personal
    data (the other is `invitations.email`); every other reference to a
    person is either a surrogate UUID foreign key or a `"user:<uuid>"`
    tagged-actor string. Both of those dereference to this row -- so
    emptying it neutralizes every one of them at once, with no need to
    rewrite `audit_logs`/`incident_timeline`/`postmortems`/`mcp_requests`
    (the first of which is explicitly append-only and must not be rewritten
    at all).

    The row itself cannot be deleted: `incidents.reported_by`,
    `postmortems.reviewed_by`, and `invitations.invited_by` are all
    `ON DELETE RESTRICT` foreign keys to it. That is the schema's own
    statement that this record is referenced by history which outlives the
    person, and it is why this module anonymizes rather than deletes.

    `password_hash` is nulled, which also permanently disables password
    login for the account; `is_active=False` blocks it at the service layer
    too (`core.users.service.resolve_identity` and
    `core.auth.service.login_with_password` both already check it -- this is
    the first code in the project to actually write it `False`).
    """
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(
            email=anonymized_email_for(user_id),
            display_name=ANONYMIZED_DISPLAY_NAME,
            password_hash=None,
            is_active=False,
        )
    )
    return int((await session.execute(stmt)).rowcount or 0)
