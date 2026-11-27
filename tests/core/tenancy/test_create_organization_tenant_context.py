"""Tenant-context ordering of `create_organization`, signup, and SSO provisioning.

Postgres RLS makes `app.current_organization_id` a per-transaction setting that
every tenant-table write must match. These tests model exactly that: a fake
"current tenant" plus fakes that RAISE if a row's organization does not match
it -- the way the real `audit_logs` / `projects` / `user_roles` policies do. The
earlier tests replaced `set_tenant_context` and the audit write with no-ops, so
they could not see a write made under the wrong context (the real bug: the
audit row for the caller's organization was written after the context had been
switched to the new organization, so `POST /organizations` failed under RLS).

Real-Postgres coverage of the same flows lives in
`scripts/verify_rls_isolation.py` (`_organization_creation_checks`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.auth import email_verification
from app.core.auth import service as auth_service
from app.core.tenancy import service as tenancy_service
from app.core.tenancy.schemas import OrganizationCreate
from app.shared.schemas import ActorKind, Identity


class _RlsModel:
    """A fake connection: one current-tenant setting, an ordered event log, and
    RLS-style checks on writes.
    """

    def __init__(self, initial_context: uuid.UUID | None) -> None:
        self.context = initial_context
        self.events: list[tuple[str, uuid.UUID | None]] = []
        self.names: dict[uuid.UUID, str] = {}

    def name(self, org: uuid.UUID | None) -> str:
        return "<unset>" if org is None else self.names.get(org, str(org))

    def check_write(self, table: str, row_org: uuid.UUID) -> None:
        if self.context != row_org:
            raise PermissionError(
                f'new row violates row-level security policy for table "{table}" '
                f"(row org={self.name(row_org)}, context={self.name(self.context)})"
            )

    def labelled_events(self) -> list[str]:
        return [f"{kind}:{self.name(org)}" for kind, org in self.events]


def _install(monkeypatch, model: _RlsModel, *, new_org_label: str = "NEW") -> uuid.UUID:
    """Patch `core.tenancy.service` with RLS-aware fakes; returns the new org id."""
    new_org_id = uuid.uuid4()
    model.names[new_org_id] = new_org_label

    async def fake_get_by_slug(session, slug):
        return None

    async def fake_insert_organization(session, *, name, slug, status="onboarding"):
        # `organizations` has no RLS policy: allowed under any context.
        model.events.append(("insert_organization", model.context))
        now = datetime.now(UTC)
        return SimpleNamespace(
            id=new_org_id, name=name, slug=slug, status="onboarding", created_at=now, updated_at=now
        )

    async def fake_set_tenant_context(session, organization_id):
        model.context = organization_id
        model.events.append(("set_context", organization_id))

    async def fake_insert_project(session, *, organization_id, name, is_default):
        model.check_write("projects", organization_id)
        model.events.append(("insert_project", organization_id))

    async def fake_record_audit_event(session, actor, **kwargs):
        model.check_write("audit_logs", actor.organization_id)
        model.events.append(("audit", actor.organization_id))

    async def fake_ensure_default_roles(session, organization_id):
        # `roles` / `role_permissions` writes are RLS-protected as well (a role is
        # only writable under its own organization's tenant context).
        model.check_write("roles", organization_id)
        model.events.append(("default_roles", organization_id))
        return []

    monkeypatch.setattr(tenancy_service.role_service, "ensure_default_roles", fake_ensure_default_roles)
    monkeypatch.setattr(tenancy_service.repository, "get_organization_by_slug", fake_get_by_slug)
    monkeypatch.setattr(tenancy_service.repository, "insert_organization", fake_insert_organization)
    monkeypatch.setattr(tenancy_service.repository, "insert_project", fake_insert_project)
    monkeypatch.setattr(tenancy_service, "set_tenant_context", fake_set_tenant_context)
    monkeypatch.setattr(tenancy_service, "record_audit_event", fake_record_audit_event)
    return new_org_id


def _admin(organization_id: uuid.UUID) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        permissions=frozenset({"tenancy:manage"}),
    )


# --- create_organization -----------------------------------------------------


@pytest.mark.asyncio
async def test_authenticated_create_organization_exact_operation_order(monkeypatch) -> None:
    caller_org = uuid.uuid4()
    model = _RlsModel(initial_context=caller_org)  # set by get_current_identity per request
    model.names[caller_org] = "CALLER"
    new_org_id = _install(monkeypatch, model)

    result = await tenancy_service.create_organization(
        None, OrganizationCreate(name="Acme", slug="acme"), actor=_admin(caller_org)
    )

    assert result.id == new_org_id
    assert model.labelled_events() == [
        "insert_organization:CALLER",  # organizations has no RLS; caller's context still set
        "audit:CALLER",  # audit row written while the caller's context is active
        "set_context:NEW",  # only then switch to the new organization
        "insert_project:NEW",  # default project under the new organization's context
        "default_roles:NEW",  # starter roles, also under the new organization's context
        "set_context:CALLER",  # caller's context restored
    ]
    assert model.context == caller_org  # the request does not stay scoped to the new org


@pytest.mark.asyncio
async def test_unauthenticated_create_organization_preserves_existing_behavior(monkeypatch) -> None:
    model = _RlsModel(initial_context=None)  # signup/SSO: no prior context
    new_org_id = _install(monkeypatch, model)

    await tenancy_service.create_organization(
        None, OrganizationCreate(name="Acme", slug="acme"), actor=None
    )

    assert model.labelled_events() == [
        "insert_organization:<unset>",
        "set_context:NEW",
        "insert_project:NEW",
        "default_roles:NEW",
    ]  # no audit, and nothing restored
    assert model.context == new_org_id  # the new org's context stays set, as signup expects


@pytest.mark.asyncio
async def test_the_original_bug_ordering_is_rejected_by_the_rls_model(monkeypatch) -> None:
    """Sanity check of the model itself: auditing AFTER the context switch --
    the old ordering -- is rejected, exactly as Postgres rejected it.
    """
    caller_org = uuid.uuid4()
    model = _RlsModel(initial_context=caller_org)
    model.names[caller_org] = "CALLER"
    new_org_id = _install(monkeypatch, model)

    await tenancy_service.set_tenant_context(None, new_org_id)
    with pytest.raises(PermissionError, match="audit_logs"):
        await tenancy_service.record_audit_event(None, _admin(caller_org), action="x")


@pytest.mark.asyncio
async def test_failed_audit_write_aborts_before_any_context_switch(monkeypatch) -> None:
    caller_org = uuid.uuid4()
    model = _RlsModel(initial_context=caller_org)
    _install(monkeypatch, model)

    async def failing_audit(session, actor, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(tenancy_service, "record_audit_event", failing_audit)

    with pytest.raises(RuntimeError):
        await tenancy_service.create_organization(
            None, OrganizationCreate(name="Acme", slug="acme"), actor=_admin(caller_org)
        )

    assert ("set_context", model.context) not in model.events[1:]  # never switched
    assert model.context == caller_org


# --- signup: creation now happens when the verification token is consumed ----


@pytest.mark.asyncio
async def test_registration_activation_sets_the_new_organizations_context_before_assign_role(
    monkeypatch,
) -> None:
    """Account creation moved from `signup` to `email_verification.verify_token`
    (nothing is created before the mailbox is proven). The RLS ordering it
    relies on is unchanged: `user_roles` is only writable under the NEW
    organization's tenant context, which must be set explicitly rather than
    depending on a side effect of `create_organization`.
    """
    new_org = uuid.uuid4()
    model = _RlsModel(initial_context=None)
    model.names[new_org] = "NEW"
    user_id, role_id = uuid.uuid4(), uuid.uuid4()
    order: list[str] = []
    registration = SimpleNamespace(
        id=uuid.uuid4(),
        kind="registration",
        email="founder@example.com",
        display_name="Founder",
        password_hash="$2b$12$hash",
        organization_name="Acme",
        organization_slug="acme",
    )

    async def fake_consume(session, *, token_hash, now):
        order.append("consume_token")
        return registration

    async def fake_create_organization(session, data, actor=None, **kwargs):
        order.append("create_organization")
        # Simulate a create_organization that does NOT leave the context set.
        return SimpleNamespace(id=new_org)

    async def fake_create_verified_user(session, *, email, display_name, password_hash, via):
        order.append(f"create_user:{via}")
        return user_id

    async def fake_ensure_admin_role(session):
        return role_id

    async def fake_set_tenant_context(session, organization_id):
        model.context = organization_id
        order.append(f"set_context:{model.name(organization_id)}")

    async def fake_assign_role(session, *, user_id, organization_id, role_id):
        model.check_write("user_roles", organization_id)  # RLS-style check
        order.append("assign_role")

    monkeypatch.setattr(email_verification.repository, "consume_verification_by_hash", fake_consume)
    monkeypatch.setattr(
        email_verification.tenancy_service, "create_organization", fake_create_organization
    )
    monkeypatch.setattr(email_verification.users_service, "create_verified_user", fake_create_verified_user)
    monkeypatch.setattr(email_verification.users_service, "ensure_admin_role", fake_ensure_admin_role)
    monkeypatch.setattr(email_verification, "set_tenant_context", fake_set_tenant_context)
    monkeypatch.setattr(email_verification.users_service, "assign_role", fake_assign_role)

    result = await email_verification.verify_token(None, "raw-token")

    assert result.status == "verified"
    assert order == [
        "consume_token",
        "create_organization",
        "create_user:email_link",
        "set_context:NEW",
        "assign_role",
    ]  # note: no session issuance step exists any more


# --- SSO provisioning --------------------------------------------------------


@pytest.mark.asyncio
async def test_sso_provisioning_sets_context_before_its_first_rls_query(monkeypatch) -> None:
    org = uuid.uuid4()
    existing_user = uuid.uuid4()
    model = _RlsModel(initial_context=None)
    model.names[org] = "ORG"

    async def fake_set_tenant_context(session, organization_id):
        model.context = organization_id
        model.events.append(("set_context", organization_id))

    async def fake_get_mapping(session, organization_id, idp_subject):
        # `external_identity_mappings` is RLS-protected: invisible without context.
        model.events.append(("get_mapping", model.context))
        if model.context != organization_id:
            return None
        return SimpleNamespace(user_id=existing_user)

    async def fake_user_state(session, user_id):
        # `users` is not RLS-protected; this lookup records no tenant event.
        return SimpleNamespace(email_verified_at=datetime.now(UTC), is_active=True)

    async def fake_holds_role(session, *, user_id, organization_id):
        return True  # `user_roles` is read under the tenant context set above

    monkeypatch.setattr(auth_service.users_service, "holds_any_role_in_organization", fake_holds_role)
    monkeypatch.setattr(auth_service, "set_tenant_context", fake_set_tenant_context)
    monkeypatch.setattr(auth_service.repository, "get_external_identity_mapping", fake_get_mapping)
    monkeypatch.setattr(auth_service.users_service, "get_user_email_state_by_id", fake_user_state)

    result = await auth_service._resolve_or_provision_user(
        None,
        organization_id=org,
        idp_subject="sub-1",
        idp_claims=SimpleNamespace(email="a@example.com", name="A", groups=(), email_verified=True),
    )

    assert result == existing_user  # the returning user is resolved, not re-provisioned
    assert model.labelled_events() == ["set_context:ORG", "get_mapping:ORG"]
