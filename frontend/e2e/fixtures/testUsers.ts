/**
 * Test accounts created by `scripts/e2e_seed.py` (Org Alpha's admin +
 * restricted user, Org Beta's admin). Passwords match exactly what that
 * script sets via the real `core.users.service.set_password` -- these
 * users log in through the genuine `POST /auth/login` path, nothing here
 * bypasses authentication.
 */
export const ORG_ALPHA = {
  slug: "e2e-org-alpha",
  // display_name is "E2E Admin" (not "...A") -- this account was first
  // provisioned by a real signup smoke-test before e2e_seed.py existed;
  // `get_or_create_user` is create-or-*fetch*, so it never renamed the
  // already-existing row.
  admin: { email: "e2e-admin-orga@example.com", password: "E2eTest123!", name: "E2E Admin" },
  restricted: { email: "e2e-restricted@example.com", password: "E2eTest123!", name: "E2E Restricted" },
};

export const ORG_BETA = {
  slug: "e2e-org-beta",
  admin: { email: "e2e-admin-orgb@example.com", password: "E2eTest123!", name: "E2E Admin B" },
};

/** Every permission code the backend actually checks anywhere (see
 * `scripts/seed_test_organization.py`'s identical list). ORG_ALPHA's admin
 * holds all of these; the restricted user holds only `incident:write`. */
export const ALL_PERMISSION_CODES = [
  "tenancy:manage",
  "incident:write",
  "postmortem:write",
  "postmortem:approve",
  "knowledge:review",
  "observability:read",
  "audit:read",
] as const;
