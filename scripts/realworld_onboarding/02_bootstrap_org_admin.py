"""02 -- Create the organization's first administrator.

PURPOSE
    A real customer's very next step after registering their organization
    (Script 01) is getting their first administrator able to configure
    SSO, invite teammates, and manage projects. This script provisions
    that identity.

WHY THIS SCRIPT DOES NOT CALL A REST ENDPOINT (a real, verified gap)
    There is no such endpoint. `POST /organizations` (Script 01) does not
    grant the caller -- or anyone -- any role in the organization it just
    created. There is also no REST or MCP endpoint anywhere in this
    codebase that creates a `Role`, creates a `Permission`, or grants a
    `Permission` to a `Role` (verified by reading every router under
    `app/api/routers/` and all of `app/core/users/service.py` directly).
    The RBAC catalog (`permissions`/`roles`/`role_permissions`) has no
    seed migration either -- nothing populates those tables at all until
    something does it directly.

    The only precedent for solving this anywhere in the existing project
    is `scripts/seed_test_organization.py`, which does exactly this for
    one hardcoded local-dev organization via direct ORM access. This
    script generalizes that same, unmodified pattern
    (`common.bootstrap`, itself built from `core.tenancy.service`,
    `core.users.service`, and `core.auth.service._issue_session` --
    the identical functions the seed script and a real SSO login both
    already use) to the organization Script 01 just registered via the
    real API.

    The resulting access token is real: normally signed, normally
    verifiable by `verify_access_token`, and accepted identically to one
    issued by a genuine SSO login by every endpoint in the system.

WHICH CODE PATH IT USES
    common.bootstrap.bootstrap_persona_sync(persona="admin") ->
        core.tenancy.service.create_organization  (idempotent -- reuses
            the org Script 01 already created)
        common.bootstrap.ensure_all_persona_roles  (creates the RBAC
            catalog + all 5 persona roles used across this whole harness)
        core.users.service.get_or_create_user / assign_role
        core.auth.service._issue_session

EXPECTED INPUT
    .state.json's "org" key, written by 01_register_org.py (falls back to
    re-deriving the org from ORG_NAME/ORG_SLUG in .env if state is
    missing, since create_organization is idempotent on slug).

EXPECTED OUTPUT
    The admin's user id, organization id, and a real access+refresh token
    pair, saved to .state.json under "users"."admin".

HOW TO EXECUTE
    python scripts/realworld_onboarding/02_bootstrap_org_admin.py

COMMON FAILURES
    - Any import error from `app.*`: run this from the project root, with
      the project's own virtualenv active (this script needs the full
      project installed, not just this harness's requirements.txt).
    - Database connection errors: the project's own DATABASE_URL (its
      OWN .env, not this harness's) must point at a reachable Postgres.
      Run `python scripts/diagnose_db_connection.py` from the repo root
      first if unsure.

EXPECTED SUCCESS OUTPUT
    "RESULT: PASS" plus a printed admin user id/organization id/token.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.bootstrap import bootstrap_persona_sync  # noqa: E402
from common.config import load_config  # noqa: E402
from common.logger import StepLogger, print_summary  # noqa: E402
from common.state import load_state, update_state  # noqa: E402

STAGE = "Create Organization Administrator"


def main() -> bool:
    config = load_config()
    log = StepLogger(STAGE)
    state = load_state()
    org_name = state.get("org", {}).get("name", config.org_name)
    org_slug = state.get("org", {}).get("slug", config.org_slug)

    log.step(f"Bootstrapping an 'admin' persona identity in organization '{org_slug}'.")
    log.info(
        "Not a REST call -- see this file's module docstring: no admin-bootstrap API "
        "exists in EKIP today. This uses the project's own service layer directly, "
        "generalizing scripts/seed_test_organization.py's existing pattern."
    )
    try:
        result = bootstrap_persona_sync(
            organization_name=org_name,
            organization_slug=org_slug,
            persona="admin",
            email=f"admin@{org_slug}.example",
            display_name="Organization Administrator",
        )
    except Exception as exc:  # noqa: BLE001
        log.failed(f"{type(exc).__name__}: {exc}")
        print_summary()
        return False

    log.info(f"Organization: {result['organization_id']} ({result['organization_slug']})")
    log.info(f"Admin user:   {result['user_id']} ({result['email']})")
    log.info(f"Access token expires in {result['expires_in']}s")

    users = load_state().get("users", {})
    users["admin"] = result
    update_state(
        org={"id": result["organization_id"], "slug": result["organization_slug"], "name": org_name},
        users=users,
    )
    log.passed(f"Admin identity ready for organization {result['organization_id']}")
    return print_summary()


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
