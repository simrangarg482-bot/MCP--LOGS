"""SQLAlchemy models for the multi-tenancy foundation.

Owned by: database/ (definition) + core/tenancy (write access), per
PROJECT_PLAN.md section 9.2. Only core/tenancy's repository code writes here;
every other module reads through core/tenancy's public interface, never by
importing these models directly -- same ownership discipline as
core_models.py.

These tables did not exist in the original single-tenant design
(DATABASE_DESIGN.md). They are defined fresh here per PROJECT_PLAN.md
section 3.2, and are the tables every other tenant-owned table will
reference (via an `organization_id` foreign key added in a follow-up change
to core_models.py) once this file lands.

Foreign keys to tables owned by other model files (`users.id`, `roles.id`)
are written as plain string references (`ForeignKey("users.id")`), not
Python class imports -- this avoids a circular import between
tenancy_models.py and core_models.py, since core_models.py will in turn need
to reference `organizations.id` once tenant columns are added there.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class Organization(Base):
    """One row per company that has purchased EKIP.

    `organization_id` (this table's primary key, referenced everywhere else)
    *is* the tenant id used throughout the system -- there is no separate
    "tenant" concept above organization (PROJECT_PLAN.md section 3.1).
    """

    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Used in the per-organization login URL (e.g. /o/{slug}/login), per
    # PROJECT_PLAN.md section 3.3.
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # onboarding / active / suspended -- the onboarding state machine
    # referenced in PROJECT_PLAN.md section 9.2.
    status: Mapped[str] = mapped_column(Text, nullable=False, default="onboarding")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Project(Base):
    """A scoping unit within an organization (e.g. "Payments team").

    Every organization has at least one project -- `is_default` marks the
    auto-created "General" project so small customers who don't need
    fine-grained scoping still have a uniform `project_id` on every incident
    and document, rather than that column being optional (PROJECT_PLAN.md
    section 3.2).
    """

    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_projects_org_name"),
        Index("ix_projects_organization_id", "organization_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SSOConfiguration(Base):
    """One row per organization, describing how its employees log in.

    All four supported providers (Entra ID, Okta, Auth0, Google Workspace)
    speak OIDC, so one row shape handles all of them -- `provider` is just a
    label; `protocol` leaves room for a future SAML-only IdP without a schema
    change (PROJECT_PLAN.md section 3.3).
    """

    __tablename__ = "sso_configurations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # One SSO config per organization for now -- unique enforces that.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)  # entra_id/okta/auth0/google_workspace
    protocol: Mapped[str] = mapped_column(Text, nullable=False, default="oidc")
    issuer_url: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    # A reference/identifier into the encrypted secret store (PROJECT_PLAN.md
    # section 12.5) -- never the raw client secret itself.
    client_secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ExternalIdentityMapping(Base):
    """Maps one IdP's subject claim to one EKIP user, within one organization.

    This is the lookup table that resolves "Continue with Microsoft" into a
    specific EKIP account (PROJECT_PLAN.md section 3.3, step 6). The unique
    constraint on (organization_id, idp_subject) is the actual login lookup
    key -- exactly one EKIP user per IdP identity per organization.
    """

    __tablename__ = "external_identity_mappings"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "idp_subject", name="uq_external_identity_org_subject"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # The IdP's stable subject claim (e.g. Entra ID's `oid`).
    idp_subject: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ConnectorConfig(Base):
    """One row per (organization, external tool) connection.

    `project_id` is nullable: a connector can be org-wide (e.g. one GitHub
    org covering every team) or scoped to a single project. `credential_ref`
    is, like `SSOConfiguration.client_secret_ref`, a reference into the
    encrypted secret store -- this table never holds a usable raw credential
    (PROJECT_PLAN.md section 12.5). `config` holds source-specific settings
    (which Slack workspace, which repos) whose shape genuinely differs per
    source -- JSONB here for the same reason `document_metadata` is EAV-style
    in the original design: fixed columns would force a migration per new
    connector.
    """

    __tablename__ = "connector_configs"
    __table_args__ = (
        Index("ix_connector_configs_org_status", "organization_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)  # slack/github/azure_devops/jira/...
    credential_ref: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="connecting")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ProjectMembership(Base):
    """Grants a user a role within one specific project.

    This is the project-level authorization tier from PROJECT_PLAN.md
    section 3.6 -- distinct from `user_roles` (organization-level role
    assignment). Reuses the existing `roles` catalog rather than inventing
    project-specific roles, so the same permission vocabulary applies at both
    the organization and project scope.
    """

    __tablename__ = "project_memberships"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
