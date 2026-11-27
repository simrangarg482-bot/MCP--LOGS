"""Persistence for core/tenancy -- organizations, projects, SSO configuration,
connector configuration.

Owned by: core/tenancy. Pure data access, same discipline as
core/audit/repository.py and core/users/repository.py: each function issues
one statement and returns ORM rows (or None/a sequence of them). No business
rules (onboarding-state transitions, "does this slug already exist") and no
ORM->Pydantic mapping live here -- that's service.py's job.

Every insert function flushes and refreshes so DB-generated columns (`id` via
gen_random_uuid(), timestamps via now()) are populated on the returned row;
the surrounding transaction is committed by the caller's session scope, not
by these functions -- same convention as core/audit/repository.py.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import case, delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.ingestion_models import IngestionJob
from app.database.models.tenancy_models import (
    ConnectorConfig,
    ExternalIdentityMapping,
    Invitation,
    Organization,
    OrganizationAccessRule,
    Project,
    SSOConfiguration,
)

# --- Organizations -----------------------------------------------------------


async def insert_organization(
    session: AsyncSession, *, name: str, slug: str, status: str = "onboarding"
) -> Organization:
    """Create one organization row and return it with server defaults populated."""
    row = Organization(name=name, slug=slug, status=status)
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_organization_by_id(
    session: AsyncSession, organization_id: uuid.UUID
) -> Organization | None:
    """Fetch a single organization by primary key, or None if absent."""
    return await session.get(Organization, organization_id)


async def list_organizations(session: AsyncSession) -> Sequence[Organization]:
    """Return every organization in the system, unscoped -- see
    `service.list_organizations`'s docstring for why this has no actor/
    organization_id filter, unlike every other query in this file.
    """
    result = await session.execute(select(Organization))
    return result.scalars().all()


async def get_organization_by_slug(
    session: AsyncSession, slug: str
) -> Organization | None:
    """Fetch a single organization by its login-URL slug, or None if absent.

    Backs the `/o/{org-slug}/login` lookup in the SSO flow -resolving which organization's
    `sso_configurations` row to redirect an employee against.
    """
    stmt = select(Organization).where(Organization.slug == slug)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_organization_status(
    session: AsyncSession, organization_id: uuid.UUID
) -> tuple[bool, str | None]:
    """`(exists, status)` of one organization, read as a COLUMN query so it is never served from
    the session's identity map: a long-running job calling this between pages must see a
    suspension committed by another session a moment ago (READ COMMITTED, fresh statement)."""
    result = await session.execute(select(Organization.status).where(Organization.id == organization_id))
    row = result.first()
    return (row is not None, row[0] if row is not None else None)


async def get_organization_access_state(
    session: AsyncSession, organization_id: uuid.UUID
) -> tuple[bool, str | None, int]:
    """`(exists, status, session_epoch)` -- fresh column query (never the identity map). Used when a
    session is issued, so the new access token is stamped with the organization's current epoch."""
    result = await session.execute(
        select(Organization.status, Organization.session_epoch).where(Organization.id == organization_id)
    )
    row = result.first()
    if row is None:
        return (False, None, 0)
    return (True, row[0], int(row[1] or 0))


async def get_organization_suspension_notice(
    session: AsyncSession, organization_id: uuid.UUID
) -> dict[str, str | None]:
    """The customer-facing facts shown to someone refused because their organization is suspended:
    its name and the operator's customer message (NEVER the reason or the private operator note, which
    live in a table this role cannot read). Only called on the already-refusing path."""
    row = (
        await session.execute(
            select(Organization.name, Organization.status_message_public).where(
                Organization.id == organization_id
            )
        )
    ).first()
    if row is None:
        return {}
    return {"organization_name": row[0], "customer_message": row[1]}


async def get_organization_statuses(
    session: AsyncSession, organization_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """`{organization_id: status}` for the given ids in ONE query (ids that do not exist are
    absent). Used by scheduled fan-outs that must filter many organizations at once."""
    if not organization_ids:
        return {}
    result = await session.execute(
        select(Organization.id, Organization.status).where(Organization.id.in_(organization_ids))
    )
    return {row[0]: row[1] for row in result.all()}


async def get_organizations_by_ids(
    session: AsyncSession, organization_ids: Sequence[uuid.UUID]
) -> Sequence[Organization]:
    """Fetch `Organization` rows for a caller-supplied set of ids, in no
    particular order -- for the multi-organization login/session work
    (`core.users.service.list_organizations_for_login`), turning a
    membership id list into the `{id, name, slug}` summaries a login/
    organizations response actually needs to show a person.

    Deliberately unscoped by any organization_id filter, same as
    `list_organizations` above -- but unlike that function this is NOT a
    system-internal "every organization" listing: the caller is expected to
    have already restricted `organization_ids` to ones a specific user
    actually belongs to (via `list_user_organization_ids`/
    `list_organizations_for_login`) before calling this. This is safe
    without `set_tenant_context` for the same reason `get_organization_by_id`
    above is: `organizations` itself carries no RLS policy, since it IS the
    tenant boundary a policy would otherwise scope by (see the `Organization`
    model's own docstring) -- there is no narrower row set to leak into.
    An empty `organization_ids` returns an empty sequence rather than every
    organization, unlike a naive unfiltered query would.
    """
    if not organization_ids:
        return []
    stmt = select(Organization).where(Organization.id.in_(organization_ids))
    result = await session.execute(stmt)
    return result.scalars().all()


# --- Projects ----------------------------------------------------------------


async def insert_project(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    name: str,
    is_default: bool = False,
) -> Project:
    """Create one project row within `organization_id` and return it."""
    row = Project(organization_id=organization_id, name=name, is_default=is_default)
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_project_by_id(
    session: AsyncSession, project_id: uuid.UUID
) -> Project | None:
    """Fetch a single project by primary key, or None if absent."""
    return await session.get(Project, project_id)


async def list_projects(
    session: AsyncSession, organization_id: uuid.UUID
) -> Sequence[Project]:
    """Return every project belonging to `organization_id`, ordered by name."""
    stmt = (
        select(Project)
        .where(Project.organization_id == organization_id)
        .order_by(Project.name)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_default_project(
    session: AsyncSession, organization_id: uuid.UUID
) -> Project | None:
    """Fetch `organization_id`'s auto-created default project, or None if it
    somehow has none yet.

    Every organization is expected to have exactly one `is_default=True`
    project (PROJECT_PLAN.md section 3.2), created alongside the organization
    itself -- this is what lets small customers get a uniform `project_id` on
    every incident/document without ever creating a second project.
    """
    stmt = select(Project).where(
        Project.organization_id == organization_id, Project.is_default.is_(True)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# --- SSO configuration ---------------------------------------------------------


async def insert_sso_configuration(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    provider: str,
    protocol: str,
    issuer_url: str,
    client_id: str,
    client_secret_ref: str,
    enabled: bool = True,
) -> SSOConfiguration:
    """Create the (single, unique-per-organization) SSO configuration row.

    The model's `unique=True` on `organization_id` is the actual "one SSO
    config per org" guarantee (PROJECT_PLAN.md section 3.2) -- this function
    does not itself check for an existing row; replacing an organization's SSO
    provider is a distinct, not-yet-built operation left to a future
    `update_sso_configuration`, not silently folded into insert.
    """
    row = SSOConfiguration(
        organization_id=organization_id,
        provider=provider,
        protocol=protocol,
        issuer_url=issuer_url,
        client_id=client_id,
        client_secret_ref=client_secret_ref,
        enabled=enabled,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def update_sso_configuration(
    session: AsyncSession, row: SSOConfiguration, changes: dict[str, Any]
) -> SSOConfiguration:
    """Apply `changes` (already validated column -> value) to `row`."""
    for column, value in changes.items():
        setattr(row, column, value)
    await session.flush()
    await session.refresh(row)
    return row


async def count_external_identity_mappings(
    session: AsyncSession, organization_id: uuid.UUID
) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(ExternalIdentityMapping)
        .where(ExternalIdentityMapping.organization_id == organization_id)
    )
    return int(result.scalar_one())


async def delete_external_identity_mappings(
    session: AsyncSession, organization_id: uuid.UUID
) -> int:
    """Drop every identity link of `organization_id`; returns how many."""
    result = await session.execute(
        delete(ExternalIdentityMapping).where(
            ExternalIdentityMapping.organization_id == organization_id
        )
    )
    await session.flush()
    return int(result.rowcount or 0)


async def get_sso_configuration_by_organization_id(
    session: AsyncSession, organization_id: uuid.UUID
) -> SSOConfiguration | None:
    """Fetch `organization_id`'s SSO configuration, or None if not yet set up.

    Backs `core/auth`'s `get_organization_sso_config(org_slug)` step in the
    login flow (PROJECT_PLAN.md section 11.1) once the caller has already
    resolved the slug to an `organization_id` via `get_organization_by_slug`.
    """
    stmt = select(SSOConfiguration).where(
        SSOConfiguration.organization_id == organization_id
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# --- Connector configuration ----------------------------------------------------


async def insert_connector_config(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    source: str,
    credential_ref: str,
    project_id: uuid.UUID | None = None,
    config: dict | None = None,
    status: str = "connecting",
) -> ConnectorConfig:
    """Create one connector configuration row and return it.

    `credential_ref` is expected to already be a valid reference into the
    encrypted secret store (PROJECT_PLAN.md section 12.5) -- this function
    never receives or stores a raw credential.
    """
    row = ConnectorConfig(
        organization_id=organization_id,
        project_id=project_id,
        source=source,
        credential_ref=credential_ref,
        config=config if config is not None else {},
        status=status,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_connector_config_by_id(
    session: AsyncSession, connector_config_id: uuid.UUID
) -> ConnectorConfig | None:
    """Fetch a single connector configuration by primary key, or None if absent."""
    return await session.get(ConnectorConfig, connector_config_id)


async def get_connector_config_by_source(
    session: AsyncSession, organization_id: uuid.UUID, source: str
) -> ConnectorConfig | None:
    """Fetch `organization_id`'s connector configuration for `source`
    (e.g. `"runbooks"`), or None if none is registered.

    Backs `core.tenancy.service.get_connector_by_source` -- the postmortem-
    approval flow's (`core.incidents` / `app.api.routers.postmortems`) way
    of finding "does this org have a runbooks connector to enqueue
    ingestion against" without a `connector_config_id` already in hand,
    unlike every other connector lookup in this module. `.first()` rather
    than `.one()`/`scalar_one_or_none()`: nothing currently enforces at
    most one connector per `(organization_id, source)`, so this stays
    defensive rather than raising if that ever changes.
    """
    stmt = select(ConnectorConfig).where(
        ConnectorConfig.organization_id == organization_id,
        ConnectorConfig.source == source,
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def list_connector_configs(
    session: AsyncSession, organization_id: uuid.UUID
) -> Sequence[ConnectorConfig]:
    """Return every connector configuration belonging to `organization_id`,
    newest first. Backs `list_connectors` (PROJECT_PLAN.md section 9.2).
    """
    stmt = (
        select(ConnectorConfig)
        .where(ConnectorConfig.organization_id == organization_id)
        .order_by(ConnectorConfig.created_at.desc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def list_ingestion_runs(
    session: AsyncSession,
    organization_id: uuid.UUID,
    connector_config_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> Sequence[IngestionJob]:
    """Return `connector_config_id`'s ingestion run history, newest first.

    Phase 2D addition backing `GET /tenancy/connectors/{id}/runs` -- reads
    the `ingestion_jobs` table directly (owned by `app.ingestion`'s data
    model, but core/tenancy is import-linter-forbidden from depending on
    `app.ingestion` itself) rather than reaching through `app.ingestion.
    service`, the same "read the raw table directly" precedent `app.
    ingestion.repository` already established for its own reads of
    `connector_configs` (this module's own table). Filtered by both
    `organization_id` and `connector_config_id` -- the caller
    (`core.tenancy.service.list_ingestion_runs`) already verified the
    connector belongs to this organization, but filtering here too costs
    nothing and means this function is never the one place a tenant-
    isolation bug could slip through.
    """
    stmt = (
        select(IngestionJob)
        .where(
            IngestionJob.organization_id == organization_id,
            IngestionJob.connector_config_id == connector_config_id,
        )
        .order_by(IngestionJob.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_ingestion_run(
    session: AsyncSession,
    organization_id: uuid.UUID,
    connector_config_id: uuid.UUID,
    job_id: uuid.UUID,
) -> IngestionJob | None:
    stmt = select(IngestionJob).where(
        IngestionJob.id == job_id,
        IngestionJob.organization_id == organization_id,
        IngestionJob.connector_config_id == connector_config_id,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_ingestion_job_stats(
    session: AsyncSession, organization_id: uuid.UUID, *, since: datetime | None = None
) -> list[Any]:
    """Aggregate `ingestion_jobs` by `connector_config_id` for
    `organization_id` : count, succeeded/failed counts, average
    duration, total documents processed -- the ingestion-side counterpart to
    `app.agents.repository.get_agent_execution_stats`/`core.observability.
    repository.get_mcp_tool_stats`. Same "read `ingestion_jobs` directly"
    precedent as `list_ingestion_runs` above (see that function's own
    docstring for why core/tenancy, not `app.ingestion`, owns this read).

    Returns raw SQLAlchemy `Row` objects -- `service.py` builds
    `IngestionJobStats` directly from these labeled columns, same
    convention as the two aggregate functions named above.
    """
    succeeded_case = case((IngestionJob.status == "succeeded", 1), else_=0)
    failed_case = case((IngestionJob.status == "failed", 1), else_=0)
    dead_lettered_case = case((IngestionJob.status == "dead_lettered", 1), else_=0)
    # Average only *successful* runs. A failed/interrupted job's `completed_at`
    # is stamped by `repository.insert_ingestion_job`'s stale-predecessor
    # recovery at the moment a replacement attempt starts -- which can be days
    # after its `started_at` -- so `completed_at - started_at` for those rows
    # is wall-clock-since-abandoned, not work time, and dragged the reported
    # average into the tens of thousands of seconds.
    duration_seconds_case = case(
        (
            (IngestionJob.status == "succeeded")
            & IngestionJob.completed_at.is_not(None)
            & IngestionJob.started_at.is_not(None),
            func.extract("epoch", IngestionJob.completed_at - IngestionJob.started_at),
        ),
        else_=None,
    )

    stmt = (
        select(
            IngestionJob.connector_config_id.label("connector_config_id"),
            func.count().label("run_count"),
            func.sum(succeeded_case).label("succeeded_count"),
            func.sum(failed_case).label("failed_count"),
            func.sum(dead_lettered_case).label("dead_lettered_count"),
            func.avg(duration_seconds_case).label("avg_duration_seconds"),
            func.sum(IngestionJob.documents_processed).label("total_documents_processed"),
            func.sum(IngestionJob.pages_fetched).label("total_pages_fetched"),
            func.sum(IngestionJob.items_discovered).label("total_items_discovered"),
            func.sum(IngestionJob.items_skipped).label("total_items_skipped"),
            func.sum(IngestionJob.chunks_embedded).label("total_chunks_embedded"),
            func.sum(IngestionJob.retry_count).label("total_retries"),
        )
        .where(IngestionJob.organization_id == organization_id)
        .group_by(IngestionJob.connector_config_id)
    )
    if since is not None:
        stmt = stmt.where(IngestionJob.created_at >= since)

    result = await session.execute(stmt)
    return list(result.all())


async def update_connector_config_sync_status(
    session: AsyncSession,
    connector_config_id: uuid.UUID,
    *,
    status: str,
    last_synced_at: datetime | None = None,
    config_patch: dict | None = None,
) -> ConnectorConfig | None:
    """Update a connector's `status` (and optionally `last_synced_at`/
    `config`) after a sync attempt, returning the updated row or None if it
    doesn't exist.

    `config_patch`, when given, is shallow-merged into the existing `config`
    JSONB (`row.config = {**row.config, **config_patch}`) rather than
    replacing it outright -- see `service.update_connector_sync_status`'s
    own docstring for why (persisting a cross-sync resume token without
    disturbing admin-supplied config keys).

    A narrow, explicit mutation rather than a generic update-by-dict: ingestion
    reporting "this connector's sync just succeeded/failed" is the only
    tenancy-owned mutation an external caller currently needs
    (PROJECT_PLAN.md section 4.5 -- job status is tracked explicitly since the
    caller and worker no longer share a call stack).

    Once `"disconnected"` (the "Delete connector" feature, `core.tenancy.
    service.disconnect_connector`), a transition to any *other* status is
    silently ignored rather than applied -- a real race, not a hypothetical
    one: `app.ingestion.service._execute_ingestion_job` only checks
    disconnected status once, at the very start of a sync, in its own
    long-lived transaction. A user can disconnect a connector *while* one of
    its syncs is already mid-flight (this check runs too early to catch
    that), and that sync's own eventual success/failure report -- this exact
    function, called with `status="active"`/`"error"` -- must not be allowed
    to silently revive a connector the user already deleted. Disconnecting
    an already-disconnected connector (the one case where `status` here
    genuinely is `"disconnected"` again) still applies normally; it's a
    same-value no-op either way.
    """
    row = await session.get(ConnectorConfig, connector_config_id)
    if row is None:
        return None
    if row.status == "disconnected" and status != "disconnected":
        return row
    row.status = status
    if last_synced_at is not None:
        row.last_synced_at = last_synced_at
    if config_patch is not None:
        row.config = {**row.config, **config_patch}
    await session.flush()
    await session.refresh(row)
    return row


async def patch_connector_config_ingestion_checkpoint(
    session: AsyncSession,
    connector_config_id: uuid.UUID,
    *,
    config_patch: dict,
) -> ConnectorConfig | None:
    """Persist high-frequency, worker-owned pagination progress.

    This intentionally does not change connector status or ``last_synced_at``
    and does not use the audited status-update path: a large source can emit
    thousands of pages, and turning each durability checkpoint into a domain
    audit event would make the audit log unusable. A disconnected connector
    is left untouched so an in-flight worker cannot mutate user-deleted
    configuration after the disconnect wins the race.
    """
    row = await session.get(ConnectorConfig, connector_config_id)
    if row is None:
        return None
    if row.status == "disconnected":
        return row
    row.config = {**row.config, **config_patch}
    await session.flush()
    await session.refresh(row)
    return row


# --- Organization access rules (domain / group auto-join) --------------------


async def insert_access_rule(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    rule_type: str,
    value: str,
    grants_role_id: uuid.UUID,
    is_active: bool = True,
) -> OrganizationAccessRule:
    """Create one access rule row and return it with server defaults populated."""
    row = OrganizationAccessRule(
        organization_id=organization_id,
        rule_type=rule_type,
        value=value,
        grants_role_id=grants_role_id,
        is_active=is_active,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_access_rule_by_id(
    session: AsyncSession, rule_id: uuid.UUID
) -> OrganizationAccessRule | None:
    """Fetch a single access rule by primary key, or None if absent."""
    return await session.get(OrganizationAccessRule, rule_id)


async def list_access_rules(
    session: AsyncSession, organization_id: uuid.UUID
) -> Sequence[OrganizationAccessRule]:
    """Return every access rule belonging to `organization_id` (active or
    not), for admin-facing listing. Evaluation-time lookups use
    `get_active_rules_by_type` instead, which filters to active rows only.
    """
    stmt = (
        select(OrganizationAccessRule)
        .where(OrganizationAccessRule.organization_id == organization_id)
        .order_by(OrganizationAccessRule.created_at.desc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_active_rules_by_type(
    session: AsyncSession, organization_id: uuid.UUID, rule_type: str
) -> Sequence[OrganizationAccessRule]:
    """Return every *active* rule of `rule_type` for `organization_id`.

    Used by `evaluate_provisioning` to check a login's email domain or IdP
    groups against the organization's configured rules -- restricted to
    `is_active=True` so a suspended rule never matches.
    """
    stmt = select(OrganizationAccessRule).where(
        OrganizationAccessRule.organization_id == organization_id,
        OrganizationAccessRule.rule_type == rule_type,
        OrganizationAccessRule.is_active.is_(True),
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def deactivate_access_rule(
    session: AsyncSession, rule_id: uuid.UUID
) -> OrganizationAccessRule | None:
    """Set an access rule's `is_active` to False, returning the updated row
    or None if it doesn't exist. Deactivating, not deleting, preserves the
    rule's history rather than losing it.
    """
    row = await session.get(OrganizationAccessRule, rule_id)
    if row is None:
        return None
    row.is_active = False
    await session.flush()
    await session.refresh(row)
    return row


# --- Invitations ---------------------------------------------------------------


async def insert_invitation(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    email: str,
    grants_role_id: uuid.UUID,
    invited_by: uuid.UUID,
    expires_at: datetime,
    token_hash: str | None = None,
    email_status: str = "not_sent",
) -> Invitation:
    """Create one invitation row (status defaults to `"pending"`, per the
    model's column default) and return it with server defaults populated.

    `token_hash` is optional -- `None` is a legitimate value,
    not an omission: see `Invitation.token_hash`'s own column comment for
    when a `NULL` token is correct (pre-Phase-7.5 rows; this codebase has no
    other caller that leaves it unset going forward).

    Does not itself enforce "at most one pending invitation per email" --
    that is the database's partial unique index
    (`uq_invitations_org_email_pending`), which raises an integrity error on
    violation; the service layer is expected to check `get_pending_invitation`
    first for a clean domain error instead of surfacing a raw database
    exception.
    """
    row = Invitation(
        organization_id=organization_id,
        email=email,
        grants_role_id=grants_role_id,
        invited_by=invited_by,
        expires_at=expires_at,
        token_hash=token_hash,
        email_status=email_status,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_invitation_by_id(
    session: AsyncSession, invitation_id: uuid.UUID
) -> Invitation | None:
    """Fetch a single invitation by primary key, or None if absent."""
    return await session.get(Invitation, invitation_id)


async def get_pending_invitation(
    session: AsyncSession, organization_id: uuid.UUID, email: str
) -> Invitation | None:
    """Fetch `email`'s pending invitation within `organization_id`, or None.

    Returns the row regardless of whether `expires_at` has already passed --
    a `status="pending"` row past its expiry simply hasn't been swept yet
    (lazy expiration, since no cleanup job exists yet); `evaluate_provisioning`
    is responsible for checking `expires_at` against the current time itself
    rather than trusting `status` alone to be up to date.
    """
    stmt = select(Invitation).where(
        Invitation.organization_id == organization_id,
        Invitation.email == email,
        Invitation.status == "pending",
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_invitations(
    session: AsyncSession, organization_id: uuid.UUID
) -> Sequence[Invitation]:
    """Return every invitation belonging to `organization_id`, newest first."""
    stmt = (
        select(Invitation)
        .where(Invitation.organization_id == organization_id)
        .order_by(Invitation.created_at.desc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def update_invitation_status(
    session: AsyncSession,
    invitation_id: uuid.UUID,
    *,
    status: str,
    accepted_at: datetime | None = None,
) -> Invitation | None:
    """Transition an invitation's status (accepted/expired/revoked), returning
    the updated row or None if it doesn't exist.

    One narrow mutation covering all three terminal transitions, rather than
    three separate functions, since they share the same shape (set `status`,
    optionally set `accepted_at`) -- mirrors
    `update_connector_config_sync_status`'s precedent above.
    """
    row = await session.get(Invitation, invitation_id)
    if row is None:
        return None
    row.status = status
    if accepted_at is not None:
        row.accepted_at = accepted_at
    await session.flush()
    await session.refresh(row)
    return row


# --- Invitation email delivery and caps -------------------------------------------------

#: First key of the two-key advisory lock serializing invitation creation for
#: one organization (the cap check is count-then-insert). Distinct from
#: `core.users.repository._ORG_ADMIN_LOCK_NAMESPACE` (7001).
_INVITATION_CAP_LOCK_NAMESPACE = 7002


async def acquire_invitation_cap_lock(session: AsyncSession, organization_id: uuid.UUID) -> None:
    """Transaction-scoped lock so two concurrent invitations cannot both pass
    the cap check (released automatically at commit/rollback)."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, hashtext(:organization_id))"),
        {"namespace": _INVITATION_CAP_LOCK_NAMESPACE, "organization_id": str(organization_id)},
    )


async def count_invitations_since(
    session: AsyncSession, organization_id: uuid.UUID, since: datetime
) -> int:
    """Invitations of any status created for `organization_id` at or after `since`."""
    stmt = (
        select(func.count())
        .select_from(Invitation)
        .where(Invitation.organization_id == organization_id, Invitation.created_at >= since)
    )
    return int((await session.execute(stmt)).scalar_one())


async def get_invitation_for_update(
    session: AsyncSession, invitation_id: uuid.UUID
) -> Invitation | None:
    """Row-locked fetch, so two concurrent resends of one invitation serialize."""
    stmt = select(Invitation).where(Invitation.id == invitation_id).with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def record_invitation_email_result(
    session: AsyncSession,
    invitation_id: uuid.UUID,
    *,
    expected_token_hash: str,
    status: str,
    attempts: int,
    now: datetime,
) -> bool:
    """Record how a delivery attempt ended -- but only if the invitation still
    carries the token this attempt was sending (`expected_token_hash`) and is
    still pending. A resend rotates the token, so a slow task for the OLD link
    can never overwrite the new delivery's state, nor mark a revoked or
    accepted invitation. Returns whether a row was updated.
    """
    values: dict[str, object] = {
        "email_status": status,
        "email_attempts": attempts,
        "email_last_attempt_at": now,
    }
    if status == "sent":
        values["email_sent_at"] = now
    result = await session.execute(
        update(Invitation)
        .where(
            Invitation.id == invitation_id,
            Invitation.token_hash == expected_token_hash,
            Invitation.status == "pending",
        )
        .values(**values)
    )
    await session.flush()
    return bool(result.rowcount)
