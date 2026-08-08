"""04 -- Create test users: Security Engineer, Developer, Manager,
Read-only User (Admin already exists from Script 02).

This script has two clearly separate halves. Read both before assuming
"create test users" means only one thing here -- in EKIP today, it can't.

--------------------------------------------------------------------------
PART 1 -- REAL API CALLS: send a real invitation for each persona
--------------------------------------------------------------------------
WHICH API IT CALLS
    POST {BASE_URL}/organizations/{organization_id}/invitations
    Body: {"email": "...", "grants_role": "<role name>"}
    Auth: Bearer <org admin token>

    This is real, unmodified `core.tenancy.service.create_invitation`,
    reached through `app/api/routers/tenancy.py`'s admin_router. It
    resolves `grants_role` (a role NAME) to a real role id, and raises
    ValidationError if the name is unknown -- which is exactly why
    Script 02 pre-creates all five persona roles (admin,
    security_engineer, developer, manager, read_only) before this script
    ever runs.

    This part is a completely legitimate exercise of the real invitation
    workflow: it genuinely stores a pending `invitations` row per persona,
    which `evaluate_provisioning` (`core.tenancy.service`) would
    genuinely honor for a real SSO login using that exact email.

--------------------------------------------------------------------------
PART 2 -- NOT AN API CALL: seed a working session for each persona
--------------------------------------------------------------------------
A REAL FINDING THIS SCRIPT SURFACES
    `POST /invitations/{id}/accept` (which IS a real, exposed REST
    endpoint) does NOT create a user or assign a role. Reading
    `core.tenancy.service.accept_invitation` directly shows it only
    flips the invitation's own `status` to "accepted" -- the user
    creation + role assignment step
    (`core.auth.service._resolve_or_provision_user`) only ever runs as
    part of a genuine SSO login completing (`complete_sso_login`), which
    itself calls `accept_invitation` as one of several side effects, not
    the other way around.

    In other words: there is no way, through any exposed API, to turn a
    pending invitation into a real, usable account without a real SSO
    login. Since this harness (by design, see the README) may be running
    with no real Identity Provider configured, Part 2 uses
    `common.bootstrap` (the same non-API mechanism Script 02 uses,
    documented in full there) to seed a real, working session for each
    of the same four personas, so Scripts 06-10 have real credentials to
    test permission/isolation/negative/logout behavior against. This is
    disclosed, not hidden: the console output below labels Part 2
    explicitly as "NOT AN API CALL".

    If you supply real IdP credentials and complete a real SSO login for
    one of these emails (see 05_login_flow.py), that login will find the
    matching pending invitation via `evaluate_provisioning` and provision
    the account for real -- at that point Part 2's seeded session for
    that persona becomes redundant, not wrong; both paths converge on the
    identical role assignment.

EXPECTED INPUT
    .state.json's "org" and "users.admin" (Scripts 01-02).

EXPECTED OUTPUT
    Four invitations created via the real API; four seeded sessions
    written to .state.json under "users".

HOW TO EXECUTE
    python scripts/realworld_onboarding/04_create_test_users.py

COMMON FAILURES
    - 422/validation error resolving `grants_role`: run
      02_bootstrap_org_admin.py first (it creates the role catalog).
    - 409 on invitation creation: an invitation for that email already
      exists and is still pending -- idempotent, treated as PASS.

EXPECTED SUCCESS OUTPUT
    Four "RESULT: PASS" lines for the invitations, four more for the
    seeded sessions.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.bootstrap import bootstrap_persona_sync  # noqa: E402
from common.config import load_config  # noqa: E402
from common.http import ApiClient, ConnectionRefused  # noqa: E402
from common.logger import StepLogger, print_summary  # noqa: E402
from common.state import load_state, update_state  # noqa: E402

PERSONAS_TO_CREATE = ["security_engineer", "developer", "manager", "read_only"]


def _email_for(org_slug: str, persona: str) -> str:
    return f"{persona}@{org_slug}.example"


def main() -> bool:
    config = load_config()
    state = load_state()
    org = state.get("org")
    admin = state.get("users", {}).get("admin")
    if not org or not admin:
        StepLogger("Create Test Users").failed(
            "Missing .state.json 'org'/'users.admin' -- run 01_register_org.py and "
            "02_bootstrap_org_admin.py first."
        )
        print_summary()
        return False

    client = ApiClient(config.base_url, config.request_timeout_seconds)
    all_ok = True

    print("\n" + "=" * 64)
    print("PART 1 -- real API calls: POST /organizations/{id}/invitations")
    print("=" * 64)
    for persona in PERSONAS_TO_CREATE:
        log = StepLogger(f"Invite {persona}")
        email = _email_for(org["slug"], persona)
        log.step(f"POST /organizations/{org['id']}/invitations for {email} (role={persona})")
        try:
            response = client.call(
                log,
                "POST",
                f"/organizations/{org['id']}/invitations",
                token=admin["access_token"],
                json_body={"email": email, "grants_role": persona},
            )
        except ConnectionRefused as exc:
            log.failed(str(exc))
            all_ok = False
            continue

        if response.status_code == 201:
            log.passed(f"Invitation created for {email}")
        elif response.status_code == 409:
            log.info("A pending invitation for this email already exists -- idempotent.")
            log.passed("Invitation already pending.")
        else:
            log.failed(f"Unexpected status {response.status_code}")
            all_ok = False

    print("\n" + "=" * 64)
    print("PART 2 -- NOT AN API CALL: seeding a working session per persona")
    print("(see this file's module docstring for exactly why this step exists)")
    print("=" * 64)
    users = load_state().get("users", {})
    for persona in PERSONAS_TO_CREATE:
        log = StepLogger(f"Seed session for {persona}")
        email = _email_for(org["slug"], persona)
        log.step(f"common.bootstrap.bootstrap_persona_sync(persona={persona!r})")
        try:
            result = bootstrap_persona_sync(
                organization_name=org["name"],
                organization_slug=org["slug"],
                persona=persona,
                email=email,
                display_name=persona.replace("_", " ").title(),
            )
        except Exception as exc:  # noqa: BLE001
            log.failed(f"{type(exc).__name__}: {exc}")
            all_ok = False
            continue
        users[persona] = result
        log.passed(f"Real session minted for {email} (user_id={result['user_id']})")

    update_state(users=users)
    print_summary()
    return all_ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
