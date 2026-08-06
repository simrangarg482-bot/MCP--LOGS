"""Persistence for core/tenancy -- organizations, projects, SSO configuration,
connector configuration.

Owned by: core/tenancy. Pure data access, same discipline as
core/audit/repository.py and core/users/repository.py: each function issues
one statement and returns ORM rows (or None/a sequence of them). No business
rules (onboarding-state transitions, "does this slug already exist") and no
ORM->Pydantic mapping live here -- that's service.py's job
(ARCHITECTURE.md section 3: infrastructure/persistence holds no business
logic).

Every insert function flushes and refreshes so DB-generated columns (`id` via
gen_random_uuid(), timestamps via now()) are populated on the returned row;
the surrounding transaction is committed by the caller's session scope, not
by these functions -- same convention as core/audit/repository.py.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.tenancy_models import (
    ConnectorConfig,
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

    Backs the `/o/{org-slug}/login` lookup in the SSO flow (PROJECT_PLAN.md
    section 3.3, section 11.1) -- resolving which organization's
    `sso_configurations` row to redirect an employee against.
    """
    stmt = select(Organization).where(Organization.slug == slug)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


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
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


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


async def update_connector_config_sync_status(
    session: AsyncSession,
    connector_config_id: uuid.UUID,
    *,
    status: str,
    last_synced_at: datetime | None = None,
) -> ConnectorConfig | None:
    """Update a connector's `status` (and optionally `last_synced_at`) after a
    sync attempt, returning the updated row or None if it doesn't exist.

    A narrow, explicit mutation rather than a generic update-by-dict: ingestion
    reporting "this connector's sync just succeeded/failed" is the only
    tenancy-owned mutation an external caller currently needs
    (PROJECT_PLAN.md section 4.5 -- job status is tracked explicitly since the
    caller and worker no longer share a call stack).
    """
    row = await session.get(ConnectorConfig, connector_config_id)
    if row is None:
        return None
    row.status = status
    if last_synced_at is not None:
        row.last_synced_at = last_synced_at
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
) -> Invitation:
    """Create one invitation row (status defaults to `"pending"`, per the
    model's column default) and return it with server defaults populated.

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
