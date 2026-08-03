"""Public interface for core/tenancy -- organizations, projects, SSO
configuration, connector configuration.

Owned by: core/tenancy. This module is the authority on "what organization/
project does this belong to, and what's connected to it" (PROJECT_PLAN.md
section 9.2). Business rules and ORM->Pydantic mapping live here; raw SQL
lives in repository.py; the wire/HTTP concerns live in the future api/ layer.

Tenant isolation (PROJECT_PLAN.md section 3.7): every function below that
takes an `organization_id` argument also takes the calling `actor: Identity`
and verifies `actor.organization_id == organization_id` before doing anything
else -- there is no operation here that lets an authenticated caller read or
write another organization's tenancy data, matching the "no admin override
query path that skips it" rule. `create_organization` is the sole exception,
since an organization does not exist yet at the moment it is being created --
see its docstring.

Authorization: mutating operations (`create_project`, `register_connector`,
`configure_sso`) additionally require the `tenancy:manage` permission via
core/users's `require_permission`, and record an audit event via
core/audit's `record_audit_event` -- the same cross-submodule dependency
pattern documented for core/incidents (PROJECT_PLAN.md section 9.4). Seeding
`tenancy:manage` into the platform's fixed permission catalog is a data
migration concern, not something this module manages.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.service import record_audit_event
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.tenancy import repository
from app.core.tenancy.schemas import (
    AccessRule,
    AccessRuleCreate,
    ConnectorConfig,
    ConnectorConfigCreate,
    Invitation,
    InvitationCreate,
    Organization,
    OrganizationCreate,
    Project,
    ProjectCreate,
    ProvisioningDecision,
    SSOConfiguration,
    SSOConfigurationCreate,
)
from app.core.users import repository as users_repository
from app.core.users.service import require_permission
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity

logger = get_logger(__name__)

_MANAGE_PERMISSION = "tenancy:manage"
# Applied when InvitationCreate.expires_at is omitted -- not yet a Settings
# field, same accepted gap as core/auth/service.py's _REFRESH_TOKEN_LIFETIME.
_DEFAULT_INVITATION_LIFETIME = timedelta(days=14)


def _ensure_same_organization(actor: Identity, organization_id: uuid.UUID) -> None:
    """Tenant-isolation guard: deny any operation scoped to an organization
    other than the caller's own (PROJECT_PLAN.md section 3.7).

    Deliberately a `PermissionDeniedError`, not a `NotFoundError` -- consistent
    with `core.users.service.require_permission`'s existing convention for
    authorization failures elsewhere in the codebase, rather than introducing
    a second denial style for this module alone.
    """
    if actor.organization_id != organization_id:
        logger.warning(
            "tenancy_cross_organization_denied",
            actor=actor.audit_tag,
            actor_organization_id=str(actor.organization_id),
            requested_organization_id=str(organization_id),
        )
        raise PermissionDeniedError(
            "Cannot access another organization's data.",
            error_code="tenancy.cross_organization_denied",
            detail={"organization_id": str(organization_id)},
        )


# --- Organizations -----------------------------------------------------------


async def create_organization(
    session: AsyncSession, data: OrganizationCreate
) -> Organization:
    """Create a new organization together with its mandatory default project.

    No `actor: Identity` parameter: an organization does not exist yet at the
    moment it is created, so there is no valid organization-scoped Identity to
    require one from (Identity.organization_id is mandatory per
    ENGINEERING_DECISIONS.md #004). This is a deliberate gap, not an oversight
    -- who/what is allowed to call this (public self-serve signup vs. an
    internal admin/sales tool) is not yet specified anywhere in the docs, and
    is left for whatever onboarding flow accompanies core/auth.

    Auto-creates the "General" default project in the same transaction
    (PROJECT_PLAN.md section 3.2: every organization has at least one project,
    so every incident/document has a uniform `project_id` even for customers
    who never create a second one).

    Raises ConflictError if `data.slug` is already taken. Note: this is a
    pre-check, not a database-constraint-driven retry -- a race between two
    concurrent signups for the same slug is a known, accepted gap (the
    `slug` column's own uniqueness constraint is the final backstop against
    actually storing a duplicate, it just wouldn't surface as this clean an
    error in that narrow race window).
    """
    existing = await repository.get_organization_by_slug(session, data.slug)
    if existing is not None:
        raise ConflictError(
            "An organization with this slug already exists.",
            error_code="organization.slug_taken",
            detail={"slug": data.slug},
        )

    org_row = await repository.insert_organization(
        session, name=data.name, slug=data.slug
    )
    await repository.insert_project(
        session, organization_id=org_row.id, name="General", is_default=True
    )

    logger.info(
        "organization_created", organization_id=str(org_row.id), slug=org_row.slug
    )
    return Organization.model_validate(org_row)


async def get_organization(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID
) -> Organization:
    """Fetch one organization. Raises NotFoundError if it doesn't exist."""
    _ensure_same_organization(actor, organization_id)

    row = await repository.get_organization_by_id(session, organization_id)
    if row is None:
        raise NotFoundError(
            "Organization not found.",
            error_code="organization.not_found",
            detail={"organization_id": str(organization_id)},
        )
    return Organization.model_validate(row)


async def get_organization_sso_config(
    session: AsyncSession, org_slug: str
) -> SSOConfiguration:
    """Resolve an organization's SSO configuration by its login-URL slug.

    Called *before* any Identity exists -- this is the very first step of the
    SSO login flow (PROJECT_PLAN.md section 3.3, section 11.1:
    `GET /o/{org-slug}/login`), so unlike every other function in this module
    it takes no `actor` and performs no tenant-isolation check: the slug
    itself is the only thing identifying which organization the employee is
    trying to log into.

    Raises NotFoundError if the slug doesn't resolve to an organization, or if
    the organization exists but has no SSO configured yet (an organization
    mid-onboarding, before an IT Admin has connected an IdP).
    """
    org_row = await repository.get_organization_by_slug(session, org_slug)
    if org_row is None:
        raise NotFoundError(
            "Organization not found.",
            error_code="organization.not_found",
            detail={"slug": org_slug},
        )

    sso_row = await repository.get_sso_configuration_by_organization_id(
        session, org_row.id
    )
    if sso_row is None:
        raise NotFoundError(
            "This organization has not configured SSO yet.",
            error_code="sso_configuration.not_found",
            detail={"organization_id": str(org_row.id)},
        )
    return SSOConfiguration.model_validate(sso_row)


async def configure_sso(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    data: SSOConfigurationCreate,
) -> SSOConfiguration:
    """Configure `organization_id`'s SSO provider for the first time.

    Raises ConflictError if SSO is already configured -- replacing an
    existing configuration is a distinct, not-yet-built operation (see
    repository.insert_sso_configuration's docstring), not silently handled
    here as an upsert.
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    existing = await repository.get_sso_configuration_by_organization_id(
        session, organization_id
    )
    if existing is not None:
        raise ConflictError(
            "SSO is already configured for this organization.",
            error_code="sso_configuration.already_exists",
            detail={"organization_id": str(organization_id)},
        )

    row = await repository.insert_sso_configuration(
        session,
        organization_id=organization_id,
        provider=data.provider,
        protocol=data.protocol,
        issuer_url=data.issuer_url,
        client_id=data.client_id,
        client_secret_ref=data.client_secret_ref,
    )
    await record_audit_event(
        session,
        actor,
        action="sso_configuration.configure",
        resource_type="sso_configuration",
        resource_id=row.id,
        metadata={"organization_id": str(organization_id), "provider": data.provider},
    )
    return SSOConfiguration.model_validate(row)


# --- Projects ----------------------------------------------------------------


async def list_projects(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID
) -> list[Project]:
    """Return every project belonging to `organization_id`."""
    _ensure_same_organization(actor, organization_id)

    rows = await repository.list_projects(session, organization_id)
    return [Project.model_validate(row) for row in rows]


async def get_default_project(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID
) -> Project:
    """Fetch `organization_id`'s auto-created default project.

    Used by callers (core/incidents, when a caller omits `project_id` on
    incident creation) that need "the project" for an organization that
    hasn't bothered creating more than one (PROJECT_PLAN.md section 3.2).
    Raises NotFoundError in the pathological case where an organization
    somehow has none -- should be unreachable given `create_organization`
    always creates one alongside the organization itself, but not re-derived
    or assumed here.
    """
    _ensure_same_organization(actor, organization_id)

    row = await repository.get_default_project(session, organization_id)
    if row is None:
        raise NotFoundError(
            "This organization has no default project.",
            error_code="project.default_missing",
            detail={"organization_id": str(organization_id)},
        )
    return Project.model_validate(row)


async def create_project(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    data: ProjectCreate,
) -> Project:
    """Create a new project within `organization_id`."""
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    row = await repository.insert_project(
        session,
        organization_id=organization_id,
        name=data.name,
        is_default=data.is_default,
    )
    await record_audit_event(
        session,
        actor,
        action="project.create",
        resource_type="project",
        resource_id=row.id,
        metadata={"organization_id": str(organization_id), "name": data.name},
    )
    return Project.model_validate(row)


# --- Connector configuration ----------------------------------------------------


async def register_connector(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    data: ConnectorConfigCreate,
) -> ConnectorConfig:
    """Register a new (organization, external tool) connection.

    If `data.project_id` is given, verifies that project actually belongs to
    `organization_id` -- without this check, a caller could otherwise scope a
    connector to a project belonging to a *different* organization, which
    would be a tenant-isolation leak at write time rather than read time.
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    if data.project_id is not None:
        project_row = await repository.get_project_by_id(session, data.project_id)
        if project_row is None or project_row.organization_id != organization_id:
            raise ValidationError(
                "project_id does not belong to this organization.",
                error_code="connector_config.invalid_project",
                detail={
                    "organization_id": str(organization_id),
                    "project_id": str(data.project_id),
                },
            )

    row = await repository.insert_connector_config(
        session,
        organization_id=organization_id,
        source=data.source,
        credential_ref=data.credential_ref,
        project_id=data.project_id,
        config=data.config,
    )
    await record_audit_event(
        session,
        actor,
        action="connector_config.register",
        resource_type="connector_config",
        resource_id=row.id,
        metadata={"organization_id": str(organization_id), "source": data.source},
    )
    return ConnectorConfig.model_validate(row)


async def list_connectors(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID
) -> list[ConnectorConfig]:
    """Return every connector configuration belonging to `organization_id`."""
    _ensure_same_organization(actor, organization_id)

    rows = await repository.list_connector_configs(session, organization_id)
    return [ConnectorConfig.model_validate(row) for row in rows]


async def update_connector_sync_status(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    connector_config_id: uuid.UUID,
    *,
    status: str,
    last_synced_at: datetime | None = None,
) -> ConnectorConfig:
    """Record a connector's sync outcome (PROJECT_PLAN.md section 4.5:
    "job status is tracked explicitly since the caller and worker no longer
    share a call stack") -- ingestion's new consumer of this module
    (app/ingestion/service.py, task #12).

    Deliberately NOT gated by `require_permission(_MANAGE_PERMISSION)`,
    unlike `register_connector`/`configure_sso`: this is a system-triggered
    completion step reporting the outcome of a job that was already
    legitimately running -- ingestion's worker calls this as
    `Identity.for_agent("ingestion_worker", organization_id)`, not on behalf
    of a human requesting a new privileged action. This mirrors
    `core.incidents.service.create_postmortem`'s reasoning for why
    persisting an already-triggered background result isn't itself
    permission-gated. `_ensure_same_organization` still applies
    unconditionally: that's a structural tenant-isolation invariant, not a
    business permission, and holds regardless of who or what is calling.
    """
    _ensure_same_organization(actor, organization_id)

    existing = await repository.get_connector_config_by_id(session, connector_config_id)
    if existing is None or existing.organization_id != organization_id:
        raise NotFoundError(
            "Connector configuration not found.",
            error_code="connector_config.not_found",
            detail={"connector_config_id": str(connector_config_id)},
        )

    row = await repository.update_connector_config_sync_status(
        session, connector_config_id, status=status, last_synced_at=last_synced_at
    )
    if row is None:
        raise RuntimeError("Connector configuration disappeared mid-update.")  # unreachable: fetched above

    await record_audit_event(
        session,
        actor,
        action="connector_config.sync_status_update",
        resource_type="connector_config",
        resource_id=connector_config_id,
        metadata={"status": status},
    )
    return ConnectorConfig.model_validate(row)


# --- Organization access rules (domain / group auto-join) --------------------


async def _resolve_role_id(session: AsyncSession, role_name: str) -> uuid.UUID:
    """Resolve a role name (as supplied on `AccessRuleCreate`/`InvitationCreate`)
    to its id, raising a clean domain error rather than letting a bad name
    surface as a foreign-key violation at insert time.
    """
    role = await users_repository.get_role_by_name(session, role_name)
    if role is None:
        raise ValidationError(
            "Unknown role name.",
            error_code="role.not_found",
            detail={"role": role_name},
        )
    return role.id


async def create_access_rule(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    data: AccessRuleCreate,
) -> AccessRule:
    """Create a domain/group auto-join rule for `organization_id`."""
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    role_id = await _resolve_role_id(session, data.grants_role)
    row = await repository.insert_access_rule(
        session,
        organization_id=organization_id,
        rule_type=data.rule_type,
        value=data.value,
        grants_role_id=role_id,
        is_active=data.is_active,
    )
    await record_audit_event(
        session,
        actor,
        action="access_rule.create",
        resource_type="organization_access_rule",
        resource_id=row.id,
        metadata={
            "organization_id": str(organization_id),
            "rule_type": data.rule_type,
            "value": data.value,
        },
    )
    return AccessRule.model_validate(row)


async def list_access_rules(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID
) -> list[AccessRule]:
    """Return every access rule (active or not) belonging to `organization_id`."""
    _ensure_same_organization(actor, organization_id)

    rows = await repository.list_access_rules(session, organization_id)
    return [AccessRule.model_validate(row) for row in rows]


async def deactivate_access_rule(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    rule_id: uuid.UUID,
) -> AccessRule:
    """Suspend an access rule without deleting it.

    Verifies the rule actually belongs to `organization_id` before touching
    it -- without this check, a caller could deactivate another
    organization's rule by guessing/enumerating its id, a write-time
    tenant-isolation leak of the same shape `register_connector` already
    guards against for `project_id`.
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    rule_row = await repository.get_access_rule_by_id(session, rule_id)
    if rule_row is None or rule_row.organization_id != organization_id:
        raise NotFoundError(
            "Access rule not found.",
            error_code="access_rule.not_found",
            detail={"organization_id": str(organization_id), "rule_id": str(rule_id)},
        )

    row = await repository.deactivate_access_rule(session, rule_id)
    await record_audit_event(
        session,
        actor,
        action="access_rule.deactivate",
        resource_type="organization_access_rule",
        resource_id=rule_id,
        metadata={"organization_id": str(organization_id)},
    )
    return AccessRule.model_validate(row)


# --- Invitations ---------------------------------------------------------------


async def create_invitation(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    data: InvitationCreate,
) -> Invitation:
    """Invite `data.email` to join `organization_id`.

    Requires `actor.user_id` to be set -- only a `USER`-kind identity (an
    actual admin) can send an invitation, since `invitations.invited_by` is a
    required reference to a `users` row; a service/agent identity has none.
    Raises ConflictError if a pending invitation for this email already
    exists (the partial unique index's application-level counterpart, for a
    clean domain error instead of a raw integrity-error surface).
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    if actor.user_id is None:
        raise ValidationError(
            "Only a user identity can send invitations.",
            error_code="invitation.invalid_actor",
        )

    existing = await repository.get_pending_invitation(session, organization_id, data.email)
    if existing is not None:
        raise ConflictError(
            "An invitation is already pending for this email.",
            error_code="invitation.already_pending",
            detail={"organization_id": str(organization_id), "email": data.email},
        )

    role_id = await _resolve_role_id(session, data.grants_role)
    expires_at = data.expires_at or (datetime.now(timezone.utc) + _DEFAULT_INVITATION_LIFETIME)

    row = await repository.insert_invitation(
        session,
        organization_id=organization_id,
        email=data.email,
        grants_role_id=role_id,
        invited_by=actor.user_id,
        expires_at=expires_at,
    )
    await record_audit_event(
        session,
        actor,
        action="invitation.create",
        resource_type="invitation",
        resource_id=row.id,
        metadata={"organization_id": str(organization_id), "email": data.email},
    )
    return Invitation.model_validate(row)


async def list_invitations(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID
) -> list[Invitation]:
    """Return every invitation belonging to `organization_id`, newest first."""
    _ensure_same_organization(actor, organization_id)

    rows = await repository.list_invitations(session, organization_id)
    return [Invitation.model_validate(row) for row in rows]


async def revoke_invitation(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    invitation_id: uuid.UUID,
) -> Invitation:
    """Revoke a pending invitation before it's accepted or expires.

    Raises ConflictError if the invitation is not currently `"pending"` --
    an already-accepted or already-expired/revoked invitation is a closed
    state machine, mirroring the `status`-transition discipline already used
    for postmortems (DATABASE_DESIGN.md).
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    invitation_row = await repository.get_invitation_by_id(session, invitation_id)
    if invitation_row is None or invitation_row.organization_id != organization_id:
        raise NotFoundError(
            "Invitation not found.",
            error_code="invitation.not_found",
            detail={"organization_id": str(organization_id), "invitation_id": str(invitation_id)},
        )
    if invitation_row.status != "pending":
        raise ConflictError(
            "Only a pending invitation can be revoked.",
            error_code="invitation.not_pending",
            detail={"status": invitation_row.status},
        )

    row = await repository.update_invitation_status(session, invitation_id, status="revoked")
    await record_audit_event(
        session,
        actor,
        action="invitation.revoke",
        resource_type="invitation",
        resource_id=invitation_id,
        metadata={"organization_id": str(organization_id)},
    )
    return Invitation.model_validate(row)


async def accept_invitation(session: AsyncSession, invitation_id: uuid.UUID) -> None:
    """Mark an invitation accepted.

    Called by core/auth only after the invited user has actually been
    created/linked -- kept as a separate, explicit step from
    `evaluate_provisioning` (which only decides whether provisioning is
    *allowed*), even though both happen inside the same database transaction
    as the rest of SSO login completion. No `actor`: this runs as part of the
    same pre-session login flow as `evaluate_provisioning`, not as an
    admin-facing action.
    """
    await repository.update_invitation_status(
        session, invitation_id, status="accepted", accepted_at=datetime.now(timezone.utc)
    )


# --- Provisioning policy evaluation ----------------------------------------------


async def evaluate_provisioning(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    email: str,
    groups: Sequence[str] = (),
) -> ProvisioningDecision:
    """Decide whether a verified SSO login may provision a user in
    `organization_id`, and which role it should receive if so.

    No `actor` parameter: this runs mid-login, before any session/Identity
    exists yet (same precedent as `get_organization_sso_config`) -- it is
    core/auth's job to call this, never the reverse, keeping "is this
    authentication valid" and "is this login authorized to provision an
    account" as two distinct steps (this migration's whole point).

    Precedence, most to least specific:
      1. A pending, unexpired invitation for this exact `email`.
      2. An active `domain` rule matching the email's domain.
      3. An active `group` rule matching one of `groups` (only checked if the
         IdP actually sent a groups claim -- an empty `groups` sequence
         means "no group claim available," not "matches no group," and
         group rules are simply skipped rather than treated as a denial
         signal on their own).
      4. Otherwise, denied.
    """
    now = datetime.now(timezone.utc)

    invitation = await repository.get_pending_invitation(session, organization_id, email)
    if invitation is not None:
        if invitation.expires_at > now:
            return ProvisioningDecision(
                allowed=True,
                grants_role_id=invitation.grants_role_id,
                matched_invitation_id=invitation.id,
                reason="invitation_match",
            )
        # Past-due but still "pending": lazily mark it expired now that
        # we've noticed (get_pending_invitation's docstring -- no sweep job
        # exists yet), then fall through to the coarser rule checks below.
        await repository.update_invitation_status(session, invitation.id, status="expired")
        logger.info(
            "invitation_expired",
            invitation_id=str(invitation.id),
            organization_id=str(organization_id),
        )

    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    if domain:
        for rule in await repository.get_active_rules_by_type(session, organization_id, "domain"):
            if rule.value.lower() == domain:
                return ProvisioningDecision(
                    allowed=True, grants_role_id=rule.grants_role_id, reason="domain_match"
                )

    if groups:
        normalized_groups = {group.lower() for group in groups}
        for rule in await repository.get_active_rules_by_type(session, organization_id, "group"):
            if rule.value.lower() in normalized_groups:
                return ProvisioningDecision(
                    allowed=True, grants_role_id=rule.grants_role_id, reason="group_match"
                )

    logger.info(
        "provisioning_denied", organization_id=str(organization_id), email=email
    )
    return ProvisioningDecision(allowed=False, reason="no_matching_policy")
