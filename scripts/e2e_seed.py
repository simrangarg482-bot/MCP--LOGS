"""Deterministic E2E test-data seeding for Playwright (frontend/e2e/**).

Creates exactly the accounts/organizations the Playwright suite needs that
cannot be created through the `POST /auth/signup` flow the suite otherwise
uses for its "one admin, one fresh org" cases:

* Org Alpha + its admin user (mirrors what `/auth/signup` does -- built the
  same way so the two are interchangeable, just provisioned up front instead
  of by the suite hitting the endpoint itself).
* A second, *restricted* user inside Org Alpha, holding a role that grants
  only `incident:write`. There is no REST endpoint or frontend flow that
  assigns a non-admin role to a second organization member today
  (`core.users.service.assign_role` takes no `actor`/permission check
  because its only real caller is SSO auto-provisioning) -- this calls the
  exact same service functions `scripts/seed_test_organization.py` already
  uses for its one admin user, just with a narrower permission set.
* Org Beta + its own admin user, for tenant-isolation checks.

Every user created this way still only ever logs in through the real
`POST /auth/login` (email+password, `core.auth.service.login_with_password`)
-- this script does not mint tokens or bypass authentication; it only
performs the account/role provisioning step no admin UI exists for yet.

Safe to re-run: every step reuses existing rows instead of duplicating them
(`get_or_create_organization`/`get_or_create_user`/`assign_role`'s own
idempotency checks).

Run: python scripts/e2e_seed.py
"""

from __future__ import annotations

import asyncio

from app.core.auth.service import _hash_password
from app.core.exceptions import ConflictError
from app.core.tenancy import repository as tenancy_repository
from app.core.tenancy import service as tenancy_service
from app.core.tenancy.schemas import Organization, OrganizationCreate
from app.core.users import repository as users_repository
from app.core.users import service as users_service
from app.database.session import session_scope, set_tenant_context
from app.shared.config.logging import configure_logging

configure_logging()

ORG_ALPHA_NAME = "E2E Org Alpha"
ORG_ALPHA_SLUG = "e2e-org-alpha"
ALPHA_ADMIN_EMAIL = "e2e-admin-orga@example.com"
# "E2E Admin", not "...A": `get_or_create_user` is create-or-*fetch*, so if
# this email already has a `users` row (e.g. from a manual signup smoke
# test before this script existed), its real `display_name` wins and is
# never renamed here.
ALPHA_ADMIN_NAME = "E2E Admin"
ALPHA_ADMIN_PASSWORD = "E2eTest123!"

ORG_BETA_NAME = "E2E Org Beta"
ORG_BETA_SLUG = "e2e-org-beta"
BETA_ADMIN_EMAIL = "e2e-admin-orgb@example.com"
BETA_ADMIN_NAME = "E2E Admin B"
BETA_ADMIN_PASSWORD = "E2eTest123!"

RESTRICTED_USER_EMAIL = "e2e-restricted@example.com"
RESTRICTED_USER_NAME = "E2E Restricted"
RESTRICTED_USER_PASSWORD = "E2eTest123!"
RESTRICTED_ROLE_NAME = "e2e_incident_writer"
# Deliberately narrow: enough to prove the RBAC suite's "has one permission,
# lacks the rest" assertions, without granting anything close to admin.
RESTRICTED_PERMISSION_CODES = ["incident:write"]


async def _get_or_create_organization(session, *, name: str, slug: str) -> Organization:
    try:
        organization = await tenancy_service.create_organization(session, OrganizationCreate(name=name, slug=slug))
        print(f"Created organization: {organization.id} ({organization.slug})")
        return organization
    except ConflictError:
        row = await tenancy_repository.get_organization_by_slug(session, slug)
        if row is None:
            raise
        organization = Organization.model_validate(row)
        print(f"Organization already exists: {organization.id} ({organization.slug})")
        return organization


async def _ensure_user_with_password(session, *, email: str, display_name: str, password: str):
    user_id = await users_service.get_or_create_user(session, email=email, display_name=display_name)
    await users_service.set_password(session, user_id=user_id, password_hash=_hash_password(password))
    return user_id


async def _ensure_admin_member(session, *, organization: Organization, email: str, display_name: str, password: str) -> None:
    admin_permissions = await users_repository.get_or_create_permissions(
        session, list(users_repository.ADMIN_PERMISSION_CODES)
    )
    admin_role = await users_repository.get_or_create_role_by_name(
        session, "admin", description="Full-access role granted to an organization's first user."
    )
    await users_repository.grant_permissions_to_role(session, role_id=admin_role.id, permissions=admin_permissions)
    user_id = await _ensure_user_with_password(session, email=email, display_name=display_name, password=password)
    await set_tenant_context(session, organization.id)
    await users_service.assign_role(session, user_id=user_id, organization_id=organization.id, role_id=admin_role.id)
    print(f"Admin user ready in {organization.slug}: {email}")


async def main() -> None:
    async with session_scope() as session:
        org_alpha = await _get_or_create_organization(session, name=ORG_ALPHA_NAME, slug=ORG_ALPHA_SLUG)
        await _ensure_admin_member(
            session, organization=org_alpha, email=ALPHA_ADMIN_EMAIL, display_name=ALPHA_ADMIN_NAME, password=ALPHA_ADMIN_PASSWORD
        )

        # Restricted second user in Org Alpha.
        permissions = await users_repository.get_or_create_permissions(session, RESTRICTED_PERMISSION_CODES)
        role = await users_repository.get_or_create_role_by_name(
            session, RESTRICTED_ROLE_NAME, description="E2E-only: incident:write and nothing else."
        )
        await users_repository.grant_permissions_to_role(session, role_id=role.id, permissions=permissions)
        restricted_user_id = await _ensure_user_with_password(
            session, email=RESTRICTED_USER_EMAIL, display_name=RESTRICTED_USER_NAME, password=RESTRICTED_USER_PASSWORD
        )
        await set_tenant_context(session, org_alpha.id)
        await users_service.assign_role(session, user_id=restricted_user_id, organization_id=org_alpha.id, role_id=role.id)
        print(f"Restricted user ready in {org_alpha.slug}: {RESTRICTED_USER_EMAIL} (role={RESTRICTED_ROLE_NAME})")

        # Org Beta + its own admin, for tenant isolation.
        org_beta = await _get_or_create_organization(session, name=ORG_BETA_NAME, slug=ORG_BETA_SLUG)
        await _ensure_admin_member(
            session, organization=org_beta, email=BETA_ADMIN_EMAIL, display_name=BETA_ADMIN_NAME, password=BETA_ADMIN_PASSWORD
        )


if __name__ == "__main__":
    asyncio.run(main())
