"""This manages EKIP's organizations, projects, SSO, and connector settings. 
It checks that users can access only their own organization's data and requires the 
`tenancy:manage` permission for changes. Changes are also recorded in audit logs. 
When a connector is registered, its password or token is encrypted before being stored. 
In short, `core/tenancy` manages organization settings, enforces access rules, and keeps 
connected services' credentials secure.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.service import record_audit_event
from app.core.exceptions import (
    OrganizationSuspendedError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    ServiceUnavailableError,
    ValidationError,
)
from app.core.tenancy import invitation_email, repository
from app.core.tenancy.live_credentials import (
    LiveConnectorCredential,
    LiveConnectorResolution,
    LiveConnectorSkip,
)
from app.core.tenancy.schemas import (
    AccessRule,
    AccessRuleCreate,
    ConnectorConfig,
    ConnectorConfigCreate,
    IngestionJobStats,
    IngestionRun,
    Invitation,
    InvitationCreate,
    Organization,
    OrganizationCreate,
    Project,
    ProjectCreate,
    ProvisioningDecision,
    SSOConfiguration,
    SSOConfigurationCreate,
    SSOConfigurationUpdate,
)
from app.core.users import role_service
from app.core.users.service import require_permission, require_project_permission
from app.database.session import set_tenant_context
from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings
from app.shared.email import email_delivery_configured
from app.shared.organization_status import is_recognized, is_suspended
from app.shared.outbound_url import UnsafeOutboundUrlError, normalize_issuer
from app.shared.schemas import Identity
from app.shared.security import (
    KmsUnavailableError,
    decrypt_secret,
    encrypt_secret,
    generate_opaque_token,
    get_kms,
    hash_opaque_token,
)

logger = get_logger(__name__)

_MANAGE_PERMISSION = "tenancy:manage"
_INCIDENT_READ_PERMISSION = "incident:read"
# Same permission as `agents.service.get_agent_execution_stats`/`core.
# observability.service.get_mcp_dashboard` -- an aggregate health dashboard
# is an observability concern, not connector configuration, so it reuses
# `observability:read` rather than `tenancy:manage` (unlike
# `list_ingestion_runs` above, whose per-connector run *history* is closer
# to a configuration drill-down and correctly reuses `_MANAGE_PERMISSION`
# instead -- these are two different questions about the same table).
_OBSERVABILITY_READ_PERMISSION = "observability:read"
# Applied when InvitationCreate.expires_at is omitted -- not yet a Settings
# field, same accepted gap as core/auth/service.py's _REFRESH_TOKEN_LIFETIME.
_DEFAULT_INVITATION_LIFETIME = timedelta(days=14)


def _ensure_same_organization(actor: Identity, organization_id: uuid.UUID) -> None:
    """Tenant-isolation guard: deny any operation scoped to an organization
    other than the caller's own.

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
    session: AsyncSession,
    data: OrganizationCreate,
    actor: Identity | None = None,
    *,
    initial_status: str = "onboarding",
) -> Organization:
    """Create a new organization together with its mandatory default project.

    `actor` is optional and defaults to `None`: an organization does not
    exist yet at the moment it is created, so there is no valid
    organization-scoped Identity to *require* one from (Identity.
    organization_id is mandatory per ENGINEERING_DECISIONS.md #004). This is
    the sole exception to this module's "every mutating operation requires
    `tenancy:manage`" rule (see module docstring) for exactly the callers
    that genuinely have no `Identity` yet: `core.auth.service.signup`'s
    public self-serve flow, and the dev-only bootstrap scripts
    (`scripts/seed_test_organization.py`, `scripts/test_milestone6.py`).
    None of these are gated here -- a brand-new customer creating their own
    first organization via signup is the intended, unauthenticated path, not
    a gap.

    A caller that *does* already have an `Identity` -- the REST
    `POST /organizations` endpoint, reached by an already-authenticated user
    creating an *additional* organization -- is a different case and is
    gated by `require_permission(actor, _MANAGE_PERMISSION)` the same as
    every other mutating operation in this module. This was previously
    unenforced (any authenticated user, in any organization, with any or no
    permissions, could call this endpoint to spin up an arbitrary new
    organization) -- a real, confirmed authorization gap, not an
    intentionally open one; the docstring here previously said so itself.
    `tenancy:manage` is checked against the actor's *own current*
    organization, since the organization being created doesn't exist yet to
    check anything against -- this doesn't by itself make the created
    organization reachable by that actor (there is still no multi-org-per-
    session capability; see PROJECT_STATUS.md's "Organization switching"
    item), it only restricts who may trigger the creation at all.
    Unauthorized callers still fail closed with the same
    `PermissionDeniedError` `require_permission` raises everywhere else.

    Auto-creates the "General" default project in the same transaction
    every organization has at least one project,
    so every incident/document has a uniform `project_id` even for customers
    who never create a second one.

    `initial_status` is `onboarding` for every caller except the verified self-service registration
    (`core.auth.email_verification._activate_registration`), which creates the organization `active`:
    an organization exists only once its creator has proven their email, so there is nothing left to
    wait for. Any other organization (an additional one created by an existing admin, an
    operator-provisioned one) stays `onboarding` until a platform operator activates it. The
    application role may INSERT a status but can never UPDATE one (migration d4a7b1c8e3f6).

    Raises ConflictError if `data.slug` is already taken. Note: this is a
    pre-check, not a database-constraint-driven retry -- a race between two
    concurrent signups for the same slug is a known, accepted gap (the
    `slug` column's own uniqueness constraint is the final backstop against
    actually storing a duplicate, it just wouldn't surface as this clean an
    error in that narrow race window).
    """
    if actor is not None:
        require_permission(actor, _MANAGE_PERMISSION)

    existing = await repository.get_organization_by_slug(session, data.slug)
    if existing is not None:
        raise ConflictError(
            "An organization with this slug already exists.",
            error_code="organization.slug_taken",
            detail={"slug": data.slug},
        )

    org_row = await repository.insert_organization(
        session, name=data.name, slug=data.slug, status=initial_status
    )

    # Tenant-context ordering matters here (verified against real Postgres
    # RLS as `ekip_app`): `app.current_organization_id` is a per-transaction
    # setting, and every tenant-owned table's policy requires a written row's
    # `organization_id` to match it.
    #
    #   1. The audit row belongs to the CALLER's organization
    #      (`actor.organization_id`), so it must be written while the
    #      caller's own context -- set per request by
    #      `api.deps.get_current_identity` -- is still active. Writing it
    #      after the switch below was rejected by the `audit_logs` policy
    #      (the row's organization != the new organization's context), which
    #      made `POST /organizations` fail and roll back entirely.
    #   2. Only then switch to the NEW organization to insert its default
    #      project (`projects` is RLS-protected too). For an unauthenticated
    #      caller (signup / SSO) there is no actor and no prior context, so
    #      this is the first and only context set.
    #   3. Restore the caller's context so the rest of an authenticated
    #      request does not silently keep running scoped to the new
    #      organization. With no actor there is nothing to restore: the new
    #      organization's context is left set, which signup relies on for its
    #      follow-up writes (and sets explicitly -- see `core.auth.service.
    #      signup`).
    if actor is not None:
        await record_audit_event(
            session,
            actor,
            action="organization.create",
            resource_type="organization",
            resource_id=org_row.id,
            metadata={"slug": org_row.slug},
        )

    await set_tenant_context(session, org_row.id)
    await repository.insert_project(
        session, organization_id=org_row.id, name="General", is_default=True
    )
    # The three editable starter roles (viewer / engineer / reviewer); written
    # under the new organization's context, which is active right here.
    await role_service.ensure_default_roles(session, org_row.id)

    if actor is not None:
        await set_tenant_context(session, actor.organization_id)

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


async def list_organizations(session: AsyncSession) -> list[Organization]:
    """Return every organization in the system, unscoped.

    No `actor` parameter, by design -- like `get_organization_sso_config`,
    this is a deliberate exception to "every operation takes an actor and is
    checked against it," not an oversight. The only legitimate caller is a
    scheduled, system-internal job that must iterate every tenant by
    definition (`app.agents.workers.tasks`'s Knowledge Gap Agent cron,
    -- mirroring `app.ingestion.workers.tasks.
    scheduled_reconciliation`'s identical precedent of calling a repository-
    level, unscoped listing directly from a worker task rather than through
    a normal actor-scoped service call). There is no narrower organization
    to scope this to when the whole point of the call is "every
    organization" -- nothing about this function is reachable from REST or
    MCP, where an actor is always available and this would be the wrong
    tool.
    """
    rows = await repository.list_organizations(session)
    return [Organization.model_validate(row) for row in rows]


def _sso_unavailable() -> NotFoundError:
    """The one error every "you cannot sign in with SSO here" cause maps to --
    unknown organization, suspended organization, no configuration, disabled
    configuration, unsupported protocol -- so the public endpoints cannot be
    used to tell those apart (organization enumeration). No detail on purpose."""
    return NotFoundError(
        "Single sign-on is not available for this organization.",
        error_code="sso.unavailable",
    )


# --- Background processing gate ------------------------------------------------------
#
# Workers and scheduled jobs never pass through `resolve_identity` (they construct their own
# `Identity.for_agent(...)` or none at all), so the "is this organization allowed to be processed"
# decision is made HERE, with the same definition of "suspended" as every access path
# (`app.shared.organization_status`). `onboarding` and `active` are processed normally.


async def assert_organization_processing_allowed(
    session: AsyncSession, organization_id: uuid.UUID
) -> None:
    """Raise `OrganizationSuspendedError` if `organization_id` is suspended, `NotFoundError` if it
    does not exist. Reads the CURRENT committed value (never a cached ORM object), so a job can
    call it again at every page / batch boundary and notice a suspension that happened mid-run."""
    exists, status = await repository.get_organization_status(session, organization_id)
    if not exists:
        raise NotFoundError(
            "Organization not found.",
            error_code="organization.not_found",
            detail={"organization_id": str(organization_id)},
        )
    if is_suspended(status):
        raise OrganizationSuspendedError(
            "This organization's access has been suspended; background processing is stopped.",
            detail={"organization_id": str(organization_id)},
        )
    if not is_recognized(status):
        logger.warning("organization_status_unrecognized", organization_id=str(organization_id))


async def list_processable_organizations(session: AsyncSession) -> list[Organization]:
    """Every organization a scheduled fan-out may enqueue work for: all except suspended ones.

    Same deliberate "no actor" exception as `list_organizations` (a system job iterating every
    tenant); it only narrows that listing."""
    return [org for org in await list_organizations(session) if not is_suspended(org.status)]


async def get_processable_organization_ids(
    session: AsyncSession, organization_ids: Sequence[uuid.UUID]
) -> set[uuid.UUID]:
    """The subset of `organization_ids` that exist and are not suspended -- one query, for fan-outs
    that start from other ids (connectors) and must drop the suspended organizations' work."""
    statuses = await repository.get_organization_statuses(session, list(organization_ids))
    return {org_id for org_id, status in statuses.items() if not is_suspended(status)}


async def get_organization_sso_config(
    session: AsyncSession, org_slug: str
) -> SSOConfiguration:
    """Resolve an organization's *usable* SSO configuration by its login-URL slug.

    Called *before* any Identity exists -- the very first step of the SSO login
    flow (`GET /auth/{org_slug}/login`) -- so it takes no `actor`: the slug alone
    identifies the organization.

    Raises `sso.unavailable` (identical for every cause, see `_sso_unavailable`)
    when the slug is unknown, the organization is suspended, it has no SSO
    configuration, the configuration is disabled, or its protocol is not OIDC
    (SAML is not implemented, so a SAML row is never treated as OIDC).

    RLS note: `organizations` carries no RLS policy, so the slug lookup needs no
    tenant context; `sso_configurations` does, so it is set the moment the
    organization id is known.
    """
    org_row = await repository.get_organization_by_slug(session, org_slug)
    if org_row is None or is_suspended(org_row.status):
        raise _sso_unavailable()

    await set_tenant_context(session, org_row.id)
    sso_row = await repository.get_sso_configuration_by_organization_id(
        session, org_row.id
    )
    if sso_row is None or not sso_row.enabled or sso_row.protocol != "oidc":
        raise _sso_unavailable()
    return SSOConfiguration.model_validate(sso_row)


_REDACTED_CLIENT_SECRET = "••••••••"  


def _redact_client_secret(config: SSOConfiguration) -> SSOConfiguration:
    """Never let a caller's actual `client_secret_ref` column value --
    envelope-encrypted ciphertext, not a human-readable reference -- reach
    the wire. Every read of an `SSOConfiguration` (both the write response
    below and `get_sso_config`) goes through this before returning.
    """
    return config.model_copy(update={"client_secret_ref": _REDACTED_CLIENT_SECRET})


async def get_sso_config(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID
) -> SSOConfiguration:
    """Read back `organization_id`'s SSO configuration for display in
    settings UI (`SsoSettingsPage.tsx`'s `getSsoConfig`, previously pointed
    at a GET endpoint that never existed on the backend at all).

    Requires `tenancy:manage` -- unlike most org-scoped reads (e.g.
    `list_projects`), viewing IdP configuration (issuer URL, client id) is
    treated the same as changing it, not as a general-membership read.
    Raises NotFoundError if SSO hasn't been configured yet (same error code
    `get_organization_sso_config`'s pre-login lookup already uses).
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    row = await repository.get_sso_configuration_by_organization_id(session, organization_id)
    if row is None:
        raise NotFoundError(
            "This organization has not configured SSO yet.",
            error_code="sso_configuration.not_found",
            detail={"organization_id": str(organization_id)},
        )
    return _redact_client_secret(SSOConfiguration.model_validate(row))


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

    `data.client_secret_ref` is envelope-encrypted before being persisted,
    the same encrypt-at-write/decrypt-at-read split `register_connector`
    already established for connector credentials -- previously this
    function stored the client secret unencrypted, and `core.auth.service.
    _resolve_client_secret` read it back as if it already were plaintext.
    Both ends of that gap are fixed together; see `_resolve_client_secret`'s
    updated docstring.
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)
    _require_oidc(data.protocol)
    issuer_url = _normalized_issuer_or_raise(data.issuer_url)
    client_id = _required_text(data.client_id, "client_id")
    if not data.client_secret_ref.strip():
        raise ValidationError("The client secret is required.", error_code="sso.client_secret_required")

    existing = await repository.get_sso_configuration_by_organization_id(
        session, organization_id
    )
    if existing is not None:
        raise ConflictError(
            "SSO is already configured for this organization.",
            error_code="sso_configuration.already_exists",
            detail={"organization_id": str(organization_id)},
        )

    try:
        encrypted_client_secret_ref = await encrypt_secret(get_kms(), data.client_secret_ref)
    except KmsUnavailableError as exc:
        # Same translation as `register_connector` above -- this call site
        # shares the exact same `encrypt_secret(get_kms(), ...)` dependency
        # and was failing identically (verified directly against production:
        # both reproduced the same unhandled-500-with-no-CORS-headers symptom).
        raise ServiceUnavailableError(
            "The SSO client secret cannot be stored right now -- the key "
            "management service is unreachable or misconfigured. Try again "
            "shortly; if this persists, it needs operator attention.",
            error_code="sso_configuration.kms_unavailable",
        ) from exc
    row = await repository.insert_sso_configuration(
        session,
        organization_id=organization_id,
        provider=data.provider,
        protocol=data.protocol,
        issuer_url=issuer_url,
        client_id=client_id,
        client_secret_ref=encrypted_client_secret_ref,
        enabled=data.enabled,
    )
    await record_audit_event(
        session,
        actor,
        action="sso_configuration.configure",
        resource_type="sso_configuration",
        resource_id=row.id,
        metadata={
            "organization_id": str(organization_id),
            "provider": data.provider,
            "enabled": data.enabled,
        },
    )
    return _redact_client_secret(SSOConfiguration.model_validate(row))


def _require_oidc(protocol: str) -> None:
    if protocol != "oidc":
        raise ValidationError(
            "Only OpenID Connect (OIDC) single sign-on is supported. SAML is not implemented.",
            error_code="sso.protocol_unsupported",
        )


def _normalized_issuer_or_raise(issuer_url: str) -> str:
    try:
        return normalize_issuer(
            issuer_url, allow_insecure=get_settings().sso_allow_insecure_issuers
        )
    except UnsafeOutboundUrlError as exc:
        raise ValidationError(
            f"The issuer URL is not acceptable: {exc} It must be an https:// URL with no "
            "query string or credentials.",
            error_code="sso.issuer_invalid",
        ) from exc


def _required_text(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValidationError(f"{field} is required.", error_code="sso.field_required")
    return cleaned


async def update_sso_config(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    data: SSOConfigurationUpdate,
) -> SSOConfiguration:
    """Change an existing SSO configuration, including switching it off/on.

    Requires `tenancy:manage` in `organization_id` (`_ensure_same_organization`
    + RLS keep it to the caller's own organization). Only supplied fields
    change; a new client secret is envelope-encrypted, an omitted one is kept.

    Changing the provider or issuer means the organization trusts a *different*
    identity provider, whose `sub` values are unrelated to the old one's -- a
    stored (organization, sub) link could then point a new, unrelated person at
    an existing account. Existing identity links are therefore dropped, but only
    when the caller explicitly confirms (`confirm_identity_reset`); otherwise
    `sso.identity_reset_required` (409) says how many links would be lost.
    Nothing is deleted on a plain disable/enable or a secret/client-id rotation.
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    row = await repository.get_sso_configuration_by_organization_id(session, organization_id)
    if row is None:
        raise NotFoundError(
            "This organization has not configured SSO yet.",
            error_code="sso_configuration.not_found",
            detail={"organization_id": str(organization_id)},
        )

    changes: dict[str, object] = {}
    identity_changed = False

    if data.provider is not None and data.provider != row.provider:
        changes["provider"] = data.provider
        identity_changed = True
    if data.issuer_url is not None:
        new_issuer = _normalized_issuer_or_raise(data.issuer_url)
        try:
            current_issuer = normalize_issuer(
                row.issuer_url, allow_insecure=get_settings().sso_allow_insecure_issuers
            )
        except UnsafeOutboundUrlError:
            current_issuer = row.issuer_url
        if new_issuer != current_issuer:
            changes["issuer_url"] = new_issuer
            identity_changed = True
    if data.client_id is not None:
        client_id = _required_text(data.client_id, "client_id")
        if client_id != row.client_id:
            changes["client_id"] = client_id
    if data.client_secret_ref is not None:
        if not data.client_secret_ref.strip():
            raise ValidationError(
                "The client secret cannot be empty.", error_code="sso.client_secret_required"
            )
        try:
            changes["client_secret_ref"] = await encrypt_secret(get_kms(), data.client_secret_ref)
        except KmsUnavailableError as exc:
            raise ServiceUnavailableError(
                "The SSO client secret cannot be stored right now -- the key "
                "management service is unreachable or misconfigured. Try again shortly.",
                error_code="sso_configuration.kms_unavailable",
            ) from exc
    if data.enabled is not None and data.enabled != row.enabled:
        changes["enabled"] = data.enabled

    cleared_links = 0
    if identity_changed:
        link_count = await repository.count_external_identity_mappings(session, organization_id)
        if link_count and not data.confirm_identity_reset:
            raise ConflictError(
                "Changing the identity provider or issuer unlinks every person who has signed "
                "in with SSO. Confirm to continue.",
                error_code="sso.identity_reset_required",
                detail={"identity_link_count": link_count},
            )
        if link_count:
            cleared_links = await repository.delete_external_identity_mappings(
                session, organization_id
            )

    if changes:
        row = await repository.update_sso_configuration(session, row, changes)
        await record_audit_event(
            session,
            actor,
            action="sso_configuration.update",
            resource_type="sso_configuration",
            resource_id=row.id,
            metadata={
                "organization_id": str(organization_id),
                # field NAMES only -- never a secret or its ciphertext
                "changed_fields": sorted(changes),
                "enabled": row.enabled,
                "identity_links_cleared": cleared_links,
            },
        )
    return _redact_client_secret(SSOConfiguration.model_validate(row))


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

_REDACTED_CREDENTIAL = "••••••••"  # noqa: RUF001 -- intentional bullet glyphs, matching _REDACTED_CLIENT_SECRET above


def _redact_credential(config: ConnectorConfig) -> ConnectorConfig:
    """Never let a caller's actual `credential_ref` column value --
    envelope-encrypted ciphertext, not a human-readable reference -- reach
    the wire. Every read of a `ConnectorConfig` goes through this before
    returning, the same treatment `_redact_client_secret` already gives
    `SSOConfiguration.client_secret_ref` (this field was the one
    inconsistency a Phase 3 security audit found: identical sensitivity,
    previously returned unredacted here).
    """
    # Underscore-prefixed values are reserved worker state (for example
    # SharePoint delta links and ingestion pagination checkpoints). Provider
    # cursors can embed bearer-like query parameters and must never be
    # serialized through the connector-management API.
    public_config = {key: value for key, value in config.config.items() if not key.startswith("_")}
    return config.model_copy(
        update={"credential_ref": _REDACTED_CREDENTIAL, "config": public_config}
    )


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
    Once validated, the `tenancy:manage` check is itself narrowed to that
    project (`require_project_permission`) rather than the organization as a
    whole -- a user granted `tenancy:manage` only on one project should not
    thereby be able to register a connector scoped to a different project in
    the same organization. A connector with no `project_id` (org-wide) still
    requires the plain org-level permission, since there is no narrower scope
    to check it against.

    `data.credential_ref` (the plaintext credential a caller submits -- e.g.
    a Slack bot token, a Jira API token pair) is envelope-encrypted (§12.5,
    `app.shared.security`) before it is ever persisted; only the encrypted
    envelope is stored, and the plaintext value is never logged or written
    to `connector_configs` directly. `ingestion.service._execute_ingestion_
    job` is the sole place that decrypts it back, immediately before a
    connector's `authenticate()` needs it -- see that function's own
    docstring.
    """
    _ensure_same_organization(actor, organization_id)

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
        require_project_permission(actor, data.project_id, _MANAGE_PERMISSION)
    else:
        require_permission(actor, _MANAGE_PERMISSION)

    try:
        encrypted_credential_ref = await encrypt_secret(get_kms(), data.credential_ref)
    except KmsUnavailableError as exc:
        # See `KmsUnavailableError`'s own docstring: without this translation
        # an unreachable/misconfigured KMS surfaced as a bare, unhandled 500
        # that never passed back through `CORSMiddleware` -- indistinguishable,
        # in a browser, from a CORS failure (this is exactly what "adding a
        # connector" was doing before this fix, for every source, not just one).
        raise ServiceUnavailableError(
            "Connector credentials cannot be stored right now -- the key "
            "management service is unreachable or misconfigured. Try again "
            "shortly; if this persists, it needs operator attention.",
            error_code="connector_config.kms_unavailable",
        ) from exc
    row = await repository.insert_connector_config(
        session,
        organization_id=organization_id,
        source=data.source,
        credential_ref=encrypted_credential_ref,
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
    return _redact_credential(ConnectorConfig.model_validate(row))


async def list_connectors(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID
) -> list[ConnectorConfig]:
    """Return every connector configuration belonging to `organization_id`.

    Gated by `tenancy:manage`, matching `register_connector`/`get_connector`
    -- a connector's `credential_ref` field is an encrypted reference, not a
    plaintext secret, but the list of what's connected and its sync status
    is still `tenancy:manage`-scoped configuration data, not something every
    org member should see by default. Previously ungated while its sibling
    write (`register_connector`) already required this permission -- a real
    inconsistency, not an intentional "reads are open" exception.
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    rows = await repository.list_connector_configs(session, organization_id)
    return [_redact_credential(ConnectorConfig.model_validate(row)) for row in rows]


async def get_connector(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    connector_config_id: uuid.UUID,
) -> ConnectorConfig:
    """Fetch one connector configuration, enforcing the same ownership and
    `tenancy:manage` permission check `register_connector` applies at write
    time -- the read-then-act counterpart needed by `POST /tenancy/
    connectors/{id}/sync` (the API layer enqueues the actual ingestion job
    itself, via its own injected `arq` pool; this function only answers
    "does this connector exist, belong to this organization, and may `actor`
    act on it," the same shape `update_connector_sync_status` already
    establishes for its own ownership check).
    """
    _ensure_same_organization(actor, organization_id)

    row = await repository.get_connector_config_by_id(session, connector_config_id)
    if row is None or row.organization_id != organization_id:
        raise NotFoundError(
            "Connector configuration not found.",
            error_code="connector_config.not_found",
            detail={"connector_config_id": str(connector_config_id)},
        )

    if row.project_id is not None:
        require_project_permission(actor, row.project_id, _MANAGE_PERMISSION)
    else:
        require_permission(actor, _MANAGE_PERMISSION)

    return _redact_credential(ConnectorConfig.model_validate(row))


async def get_connector_by_source(
    session: AsyncSession, organization_id: uuid.UUID, source: str
) -> ConnectorConfig | None:
    """Fetch `organization_id`'s connector configuration for `source`
    (e.g. `"runbooks"`), or None if none is registered.

    Deliberately no `actor`/permission check, unlike `get_connector` above:
    this is an internal existence lookup used by the postmortem-approval
    flow (`app.api.routers.postmortems.approve_postmortem`) to decide
    whether to enqueue a background ingestion job, not a read exposed back
    to the caller -- the action it gates was already authorized by
    `postmortem:approve` on the approval itself, and the raw
    `ConnectorConfig` (including its encrypted `credential_ref`) never
    reaches the client from this path, so `_redact_credential` is not
    applied here either.
    """
    row = await repository.get_connector_config_by_source(session, organization_id, source)
    if row is None:
        return None
    return ConnectorConfig.model_validate(row)


async def resolve_live_evidence_connectors(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    *,
    incident_organization_id: uuid.UUID,
    incident_project_id: uuid.UUID,
    sources: Collection[str],
    not_implemented_sources: Collection[str] = (),
) -> LiveConnectorResolution:
    """Internal read for the Investigation Agent's live lookups: the
    organization's active connectors for `sources`, with credentials
    decrypted in memory.

    This is deliberately a different operation from `list_connectors`:
    `list_connectors` is the *management* view (needs `tenancy:manage`,
    always redacted, safe for the API). This function is for *using* a
    connector on behalf of an investigation, so it is gated on the right to
    read the incident being investigated (`incident:read` on its project)
    rather than on `tenancy:manage`. It must never be exposed through a
    router or MCP tool, and its result must never be returned or stored.

    Enforced here (not merely trusted from the caller):
      - same-organization guard on `organization_id` and on the incident;
      - `incident:read` for `actor` on `incident_project_id`;
      - only `status == "active"` connectors whose `source` is in `sources`;
      - only connectors that are org-wide (`project_id is None`) or scoped to
        the incident's own project -- another project's token is never used.

    Connectors whose `source` is in `not_implemented_sources` (placeholder
    live sources with no real backend) get the same org/status/project
    filtering but are reported as skipped with reason `"not_implemented"`
    WITHOUT decrypting their credential -- a secret is never loaded for a
    source that cannot use it.

    A connector whose credential cannot be decrypted (corrupt/tampered
    envelope, KMS unavailable) is skipped with a fixed-vocabulary reason and
    never aborts the others. Raw exception text is never kept: it could echo
    ciphertext details.
    """
    _ensure_same_organization(actor, organization_id)
    if incident_organization_id != organization_id:
        logger.warning(
            "tenancy_live_connectors_cross_organization_denied",
            actor=actor.audit_tag,
            actor_organization_id=str(actor.organization_id),
        )
        raise PermissionDeniedError(
            "Cannot access another organization's data.",
            error_code="tenancy.cross_organization_denied",
            detail={"organization_id": str(incident_organization_id)},
        )
    require_project_permission(actor, incident_project_id, _INCIDENT_READ_PERMISSION)

    credentials: list[LiveConnectorCredential] = []
    skipped: list[LiveConnectorSkip] = []
    rows = await repository.list_connector_configs(session, organization_id)
    for row in rows:
        if row.organization_id != organization_id:
            continue  # belt and braces on top of the org filter and RLS
        if row.status != "active":
            continue
        if row.source not in sources and row.source not in not_implemented_sources:
            continue
        if row.project_id is not None and row.project_id != incident_project_id:
            continue
        if row.source in not_implemented_sources and row.source not in sources:
            skipped.append(
                LiveConnectorSkip(
                    connector_config_id=row.id, source=row.source, reason="not_implemented"
                )
            )
            continue

        try:
            token = await decrypt_secret(get_kms(), row.credential_ref)
        except Exception as exc:  # noqa: BLE001 -- see docstring: skip, never abort or leak
            reason = "credential_unavailable"
            if isinstance(exc, KmsUnavailableError):
                reason = "kms_unavailable"
            logger.warning(
                "tenancy_live_connector_credential_unavailable",
                connector_config_id=str(row.id),
                source=row.source,
                error_type=type(exc).__name__,
            )
            skipped.append(
                LiveConnectorSkip(connector_config_id=row.id, source=row.source, reason=reason)
            )
            continue
        if not token:
            skipped.append(
                LiveConnectorSkip(
                    connector_config_id=row.id, source=row.source, reason="credential_unavailable"
                )
            )
            continue

        credentials.append(
            LiveConnectorCredential(
                connector_config_id=row.id,
                organization_id=row.organization_id,
                project_id=row.project_id,
                source=row.source,
                # Same rule as `_redact_credential`: underscore keys are
                # reserved worker state (cursors) and never leave this module.
                config={k: v for k, v in (row.config or {}).items() if not k.startswith("_")},
                token=token,
            )
        )

    return LiveConnectorResolution(credentials=credentials, skipped=skipped)


async def list_ingestion_runs(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    connector_config_id: uuid.UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[IngestionRun]:
    """List `connector_config_id`'s ingestion run history, newest first.

    Phase 2D addition -- backs `GET /tenancy/connectors/{id}/runs`, the
    frontend's ingestion-monitoring page. No new permission was introduced:
    `tenancy:manage` already gates everything else about a connector
    (`register_connector`, `list_connectors`, `get_connector`) -- ingestion
    run history is the same kind of connector-configuration-adjacent data,
    not a separately-owned resource, so reusing the existing permission is
    correct per this codebase's own "don't invent a permission a real one
    already covers" convention. `get_connector` already applies exactly the
    ownership + permission check this needs, so this reuses it rather than
    duplicating that check a third time.
    """
    connector = await get_connector(session, actor, organization_id, connector_config_id)
    rows = await repository.list_ingestion_runs(
        session, organization_id, connector.id, limit=limit, offset=offset
    )
    return [IngestionRun.model_validate(row) for row in rows]


async def get_replayable_ingestion_run(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    connector_config_id: uuid.UUID,
    job_id: uuid.UUID,
) -> IngestionRun:
    """Authorize an explicit replay of one exhausted ingestion attempt."""
    connector = await get_connector(session, actor, organization_id, connector_config_id)
    row = await repository.get_ingestion_run(
        session, organization_id, connector.id, job_id
    )
    if row is None:
        raise NotFoundError(
            "Ingestion run not found.",
            error_code="ingestion_job.not_found",
            detail={"job_id": str(job_id)},
        )
    run = IngestionRun.model_validate(row)
    if run.status != "dead_lettered":
        raise ConflictError(
            "Only a dead-lettered ingestion run can be replayed.",
            error_code="ingestion_job.not_replayable",
            detail={"job_id": str(job_id), "status": run.status},
        )
    return run


async def get_ingestion_job_stats(
    session: AsyncSession, actor: Identity, *, since: datetime | None = None
) -> list[IngestionJobStats]:
    """Per-connector ingestion run aggregate for `actor.organization_id`
    (Phase 5.6) -- backs `GET /observability/ingestion`, the
    `ingestion_jobs`-side counterpart to `agents.service.
    get_agent_execution_stats`/`core.observability.service.
    get_mcp_dashboard`. See `_OBSERVABILITY_READ_PERMISSION`'s own comment
    for why this reuses `observability:read`, not `tenancy:manage`.
    """
    require_permission(actor, _OBSERVABILITY_READ_PERMISSION)
    rows = await repository.get_ingestion_job_stats(session, organization_id=actor.organization_id, since=since)
    return [
        IngestionJobStats(
            connector_config_id=row.connector_config_id,
            run_count=row.run_count,
            succeeded_count=int(row.succeeded_count or 0),
            failed_count=int(row.failed_count or 0),
            dead_lettered_count=int(getattr(row, "dead_lettered_count", 0) or 0),
            avg_duration_seconds=(
                float(row.avg_duration_seconds) if row.avg_duration_seconds is not None else None
            ),
            total_documents_processed=int(row.total_documents_processed or 0),
            total_pages_fetched=int(getattr(row, "total_pages_fetched", 0) or 0),
            total_items_discovered=int(getattr(row, "total_items_discovered", 0) or 0),
            total_items_skipped=int(getattr(row, "total_items_skipped", 0) or 0),
            total_chunks_embedded=int(getattr(row, "total_chunks_embedded", 0) or 0),
            total_retries=int(getattr(row, "total_retries", 0) or 0),
        )
        for row in rows
    ]


async def update_connector_sync_status(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    connector_config_id: uuid.UUID,
    *,
    status: str,
    last_synced_at: datetime | None = None,
    config_patch: dict | None = None,
) -> ConnectorConfig:
    """Record a connector's sync outcome (PROJECT_PLAN.md section 4.5:
    "job status is tracked explicitly since the caller and worker no longer
    share a call stack") -- ingestion's new consumer of this module
    (app/ingestion/service.py, task #12).

    `config_patch`, when given, is shallow-merged into the connector's
    existing `config` JSONB rather than replacing it -- ingestion's own
    caller uses this to persist a cross-sync resume token (`FetchResult.
    resume_token`, see that field's docstring) under a reserved `
    "_resume_token"` key without disturbing the admin-supplied keys
    (`site_ids`/`channels`/...) already living in the same JSONB blob.

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
        session,
        connector_config_id,
        status=status,
        last_synced_at=last_synced_at,
        config_patch=config_patch,
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
    return _redact_credential(ConnectorConfig.model_validate(row))


async def checkpoint_connector_sync(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    connector_config_id: uuid.UUID,
    *,
    config_patch: dict,
) -> ConnectorConfig:
    """Durably save an ingestion cursor without emitting an audit event.

    Status changes remain audited through :func:`update_connector_sync_status`.
    This separate system-only path exists because pagination checkpoints are
    operational state written after every completed page, not user/domain
    actions. Tenant ownership is still checked at both the service and query
    boundaries.
    """
    _ensure_same_organization(actor, organization_id)
    existing = await repository.get_connector_config_by_id(session, connector_config_id)
    if existing is None or existing.organization_id != organization_id:
        raise NotFoundError(
            "Connector configuration not found.",
            error_code="connector_config.not_found",
            detail={"connector_config_id": str(connector_config_id)},
        )
    row = await repository.patch_connector_config_ingestion_checkpoint(
        session, connector_config_id, config_patch=config_patch
    )
    if row is None:
        raise RuntimeError("Connector configuration disappeared mid-update.")
    return _redact_credential(ConnectorConfig.model_validate(row))


async def disconnect_connector(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    connector_config_id: uuid.UUID,
) -> ConnectorConfig:
    """User-facing "delete a connector" (`DELETE /tenancy/connectors/{id}`).

    A real hard `DELETE` on `connector_configs` is not available: `ingestion_
    jobs.connector_config_id` is `ON DELETE RESTRICT` (deliberately, to
    protect a connector's own sync/job history from disappearing along with
    it), so Postgres itself refuses to drop a row that has ever been synced
    even once -- which in practice is nearly every connector a user would
    ever want to remove. `ConnectorStatus` already defines `"disconnected"`
    for exactly this case (present in the schema/type since Milestone 9's
    original design, never previously reachable from any code path) -- this
    reuses `update_connector_config_sync_status` (the same repository
    function ingestion's own worker calls to report sync outcomes) to set
    it, rather than inventing a second update path.

    Two real, load-bearing effects of the resulting `"disconnected"` status,
    not just a cosmetic label:
      1. `list_active_connector_config_ids()` (`d2e5f8a3c1b6`) -- the
         `SECURITY DEFINER` function `scheduled_reconciliation`'s hourly cron
         pass uses to pick which connectors to re-sync -- only ever selects
         `'active'`/`'error'` rows. A disconnected connector is silently
         excluded from all future automatic reconciliation, with no
         additional code needed here.
      2. `app.ingestion.service._execute_ingestion_job` checks this status
         immediately after loading the connector row and refuses to proceed
         (a fast `ConflictError`, not a full sync attempt) -- covering both
         a stale manual "Sync now" already in flight when this call lands,
         and, more importantly, any `arq` retry already queued for a job
         that timed out before the user disconnected it: the retry still
         fires, but now fails in milliseconds instead of running the same
         slow sync to the same timeout a second and third time.

    Reachable from the UI as a card-level "Delete" action even though the
    row survives -- the frontend's own connector list filters `"disconnected"`
    rows out of the default view (see `ConnectorsPage.tsx`), so this reads
    as "gone" to the user while `ingestion_jobs`/`documents` history stays
    intact for anyone with direct database/audit-log access.
    """
    connector = await get_connector(session, actor, organization_id, connector_config_id)

    row = await repository.update_connector_config_sync_status(
        session, connector.id, status="disconnected"
    )
    if row is None:
        raise RuntimeError("Connector configuration disappeared mid-update.")  # unreachable: fetched above

    await record_audit_event(
        session,
        actor,
        action="connector_config.disconnect",
        resource_type="connector_config",
        resource_id=connector_config_id,
    )
    return _redact_credential(ConnectorConfig.model_validate(row))


# --- Organization access rules (domain / group auto-join) --------------------


async def _resolve_role_id(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, role_name: str
) -> uuid.UUID:
    """Resolve a role name (as supplied on `AccessRuleCreate`/`InvitationCreate`)
    to its id among the roles `organization_id` may use -- its own custom roles
    first, then the built-ins -- raising a clean domain error rather than
    letting a bad name surface as a foreign-key violation at insert time.

    Also refuses a role carrying permissions `actor` does not hold themselves
    (`role.permission_escalation`): handing out a role is granting its
    permissions, so an administrator cannot invite or auto-admit someone into a
    role more powerful than their own.
    """
    role_id = await role_service.resolve_role_id(session, organization_id, role_name)
    await role_service.assert_actor_can_grant_role(session, actor, organization_id, role_id)
    return role_id


async def create_access_rule(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    data: AccessRuleCreate,
) -> AccessRule:
    """Create a domain/group auto-join rule for `organization_id`."""
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    role_id = await _resolve_role_id(session, actor, organization_id, data.grants_role)
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
    """Return every access rule (active or not) belonging to `organization_id`.

    Gated by `tenancy:manage`, matching `create_access_rule`/
    `deactivate_access_rule` -- previously ungated, a real inconsistency
    (see `list_connectors`'s identical fix above).
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

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

    generates a random, single-use token, stores only its hash
    (`token_hash`), and returns the raw value exactly once via the response
    `Invitation.token` field -- the caller (an admin UI, or whatever sends
    the actual invitation email) is responsible for including it in the
    invitation link. This is what `POST /invitations/{id}/accept`'s
    password-organization flow (`core.auth.service.
    accept_invitation_with_password`) checks as proof the accepter controls
    the invited email address -- previously the invitation's own database id
    was the only "token," provable by anyone who merely learned it.
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

    role_id = await _resolve_role_id(session, actor, organization_id, data.grants_role)
    expires_at = data.expires_at or (datetime.now(timezone.utc) + _DEFAULT_INVITATION_LIFETIME)

    raw_token = generate_opaque_token()
    row = await repository.insert_invitation(
        session,
        organization_id=organization_id,
        email=data.email,
        grants_role_id=role_id,
        invited_by=actor.user_id,
        expires_at=expires_at,
        token_hash=hash_opaque_token(raw_token),
    )
    await record_audit_event(
        session,
        actor,
        action="invitation.create",
        resource_type="invitation",
        # Never the raw token -- see this function's own docstring on why
        # it must be exposed exactly once, in the response only.
        metadata={"organization_id": str(organization_id), "email": data.email},
        resource_id=row.id,
    )
    return Invitation.model_validate(row).model_copy(update={"token": raw_token})


@dataclass(frozen=True)
class IssuedInvitation:
    """Result of issuing or re-issuing an invitation.

    `pending_email` is set when a mail is owed -- the caller schedules
    `invitation_email.deliver_invitation_email(pending_email)` to run after the
    response. `invitation.token` (the raw, single-use secret) is set ONLY when
    the admin has to hand the link over themselves: email is not configured, or
    they explicitly asked for a fresh link. When the mail is going out, the
    token never leaves the server except inside that mail.
    """

    invitation: Invitation
    pending_email: invitation_email.PendingInvitationEmail | None


async def _enforce_invitation_caps(
    session: AsyncSession, organization_id: uuid.UUID, now: datetime
) -> None:
    """At most `invitation_max_per_hour` / `invitation_max_per_day` NEW
    invitations per organization. Counted from the database (not Redis), so the
    limit holds across API replicas and never fails open; the advisory lock
    makes count-then-insert atomic per organization."""
    settings = get_settings()
    await repository.acquire_invitation_cap_lock(session, organization_id)
    for window, delta, limit, code in (
        ("hour", timedelta(hours=1), settings.invitation_max_per_hour, "invitation.hourly_limit"),
        ("day", timedelta(days=1), settings.invitation_max_per_day, "invitation.daily_limit"),
    ):
        if await repository.count_invitations_since(session, organization_id, now - delta) >= limit:
            raise RateLimitedError(
                f"This organization has reached its limit of {limit} invitations per {window}. "
                "Please try again later.",
                error_code=code,
                detail={"limit": limit, "window": window},
            )


async def issue_invitation(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    data: InvitationCreate,
) -> IssuedInvitation:
    """Create an invitation and, when an email provider exists, queue the email.

    Same authorization and validation as `create_invitation` (which stays as the
    plain, non-emailing primitive for scripts), plus: per-organization hourly /
    daily caps, and delivery state on the row -- `queued` when a mail is owed,
    `not_configured` when it is not and the admin must copy the link.

    The response is identical whether or not `data.email` already belongs to an
    account: nothing here looks the address up.
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)
    if actor.user_id is None:
        raise ValidationError(
            "Only a user identity can send invitations.", error_code="invitation.invalid_actor"
        )

    now = datetime.now(timezone.utc)
    await _enforce_invitation_caps(session, organization_id, now)

    existing = await repository.get_pending_invitation(session, organization_id, data.email)
    if existing is not None:
        raise ConflictError(
            "An invitation is already pending for this email.",
            error_code="invitation.already_pending",
            detail={"organization_id": str(organization_id), "email": data.email},
        )

    role_id = await _resolve_role_id(session, actor, organization_id, data.grants_role)
    expires_at = data.expires_at or (now + _DEFAULT_INVITATION_LIFETIME)
    emailing = email_delivery_configured()

    raw_token = generate_opaque_token()
    row = await repository.insert_invitation(
        session,
        organization_id=organization_id,
        email=data.email,
        grants_role_id=role_id,
        invited_by=actor.user_id,
        expires_at=expires_at,
        token_hash=hash_opaque_token(raw_token),
        email_status="queued" if emailing else "not_configured",
    )
    if emailing:
        row.email_last_attempt_at = now  # the resend cooldown starts when the mail is queued
        await session.flush()
        await session.refresh(row)  # `updated_at` is regenerated by the UPDATE
    await record_audit_event(
        session,
        actor,
        action="invitation.create",
        resource_type="invitation",
        metadata={
            "organization_id": str(organization_id),
            "email": data.email,
            "email_status": row.email_status,
        },
        resource_id=row.id,
    )

    pending = (
        await invitation_email.prepare_invitation_email(session, actor, row, raw_token, origin="create")
        if emailing
        else None
    )
    invitation = Invitation.model_validate(row).model_copy(
        update={"token": None if emailing else raw_token}
    )
    return IssuedInvitation(invitation=invitation, pending_email=pending)


async def resend_invitation(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    invitation_id: uuid.UUID,
    *,
    method: str = "email",
) -> IssuedInvitation:
    """Re-issue a pending invitation: ROTATE its token (the previous link stops
    working) and either email the new link (`method="email"`) or return it once
    for the admin to copy (`method="link"`).

    Email resends are bounded: a cooldown between sends and a maximum count per
    invitation. A fresh link also pushes the expiry out to the default lifetime
    if it would otherwise be sooner (an invitation that lapsed can be revived).
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)
    settings = get_settings()

    row = await repository.get_invitation_for_update(session, invitation_id)
    if row is None or row.organization_id != organization_id:
        raise NotFoundError(
            "Invitation not found.",
            error_code="invitation.not_found",
            detail={"invitation_id": str(invitation_id)},
        )
    if row.status != "pending":
        raise ConflictError(
            "This invitation is no longer pending.",
            error_code="invitation.not_pending",
            detail={"status": row.status},
        )

    now = datetime.now(timezone.utc)
    emailing = method == "email"
    if emailing:
        if not email_delivery_configured():
            raise ServiceUnavailableError(
                "Email delivery is not configured. Copy a new link instead.",
                error_code="email.not_configured",
            )
        if row.resend_count >= settings.invitation_max_resends:
            raise RateLimitedError(
                f"This invitation has already been re-sent {settings.invitation_max_resends} times.",
                error_code="invitation.resend_limit",
                detail={"limit": settings.invitation_max_resends},
            )
        last = row.email_last_attempt_at
        cooldown = timedelta(seconds=settings.invitation_resend_cooldown_seconds)
        if last is not None and now - last < cooldown:
            wait = int((cooldown - (now - last)).total_seconds()) + 1
            raise RateLimitedError(
                f"Please wait {wait} seconds before re-sending this invitation.",
                error_code="invitation.resend_cooldown",
                detail={"retry_after_seconds": wait},
            )

    raw_token = generate_opaque_token()
    row.token_hash = hash_opaque_token(raw_token)
    refreshed_expiry = now + _DEFAULT_INVITATION_LIFETIME
    if row.expires_at < refreshed_expiry:
        row.expires_at = refreshed_expiry
    if emailing:
        row.resend_count += 1
        row.email_status = "queued"
        row.email_attempts = 0
        row.email_last_attempt_at = now  # starts the cooldown at queue time
    else:
        row.email_status = "not_sent"  # the mail already sent carries a link that no longer works
    await session.flush()
    await session.refresh(row)

    await record_audit_event(
        session,
        actor,
        action="invitation.resend",
        resource_type="invitation",
        resource_id=row.id,
        metadata={"organization_id": str(organization_id), "method": method},
    )
    pending = (
        await invitation_email.prepare_invitation_email(session, actor, row, raw_token, origin="resend")
        if emailing
        else None
    )
    invitation = Invitation.model_validate(row).model_copy(
        update={"token": None if emailing else raw_token}
    )
    return IssuedInvitation(invitation=invitation, pending_email=pending)


async def list_invitations(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID
) -> list[Invitation]:
    """Return every invitation belonging to `organization_id`, newest first.

    Gated by `tenancy:manage`, matching `create_invitation`/
    `revoke_invitation` -- previously ungated, which leaked every invited
    email address in the organization to any authenticated org member (see
    `list_connectors`'s identical fix above for the same class of bug).
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

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

    Guards added for its second caller, `auth_service.
    accept_invitation_with_password  -- itself behind
    `POST /invitations/{invitation_id}/accept`, which verifies the caller's
    `token` against `Invitation.token_hash` before ever reaching here:
    raises NotFoundError for an unknown id, and ConflictError if the
    invitation is not currently `"pending"` (already accepted/revoked) or has
    passed its `expires_at`. `evaluate_provisioning` only ever passes an id it
    just confirmed satisfies both conditions, so these checks are a no-op,
    defense-in-depth addition for that call path, not a behavior change for
    it; for the password-acceptance path they're deliberately re-checked here
    too, rather than trusted to still hold from the caller's own earlier
    check, in case anything changed between the two.

    Does not itself check `token_hash` -- that proof-of-control check happens
    once, in `auth_service.accept_invitation_with_password`, before user
    provisioning even starts. This function only flips `status`; it has no
    token to check for its other caller (`evaluate_provisioning`'s SSO path),
    where a signed `id_token` already proved identity.
    """
    invitation = await repository.get_invitation_by_id(session, invitation_id)
    if invitation is None:
        raise NotFoundError(
            "Invitation not found.",
            error_code="invitation.not_found",
            detail={"invitation_id": str(invitation_id)},
        )
    if invitation.status != "pending":
        raise ConflictError(
            "Only a pending invitation can be accepted.",
            error_code="invitation.not_pending",
            detail={"status": invitation.status},
        )
    if invitation.expires_at <= datetime.now(timezone.utc):
        raise ConflictError(
            "This invitation has expired.",
            error_code="invitation.expired",
            detail={"invitation_id": str(invitation_id)},
        )

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
    """This function decides whether someone logging in through SSO is allowed to join 
    an organization and which role they should get. It checks in this order: (1) a valid 
    invitation for their email, (2) an active rule matching their email domain, (3) an 
    active rule matching their identity provider's groups, if group information is 
    available. If none match, access is denied. It runs before login finishes, so it 
    doesn't need an existing user identity. It also sets the organization context before 
    querying protected database tables, allowing Row-Level Security (RLS) to work 
    correctly.
    """
    await set_tenant_context(session, organization_id)
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
