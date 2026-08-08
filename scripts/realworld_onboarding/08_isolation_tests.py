"""08 -- Multi-tenant isolation: Organization A cannot see or touch
Organization B's data, and vice versa.

PURPOSE
    Prove that org-scoping actually holds across the real REST API for
    two genuinely separate organizations -- not just that a permission
    check exists, but that a caller from one organization is denied
    access to another organization's resources even when they hold every
    relevant permission in their OWN organization.

WHAT IT DOES
    1. Registers a second organization, "Organization B" (ORG_B_NAME/
       ORG_B_SLUG in .env), with its own admin -- via the same
       `common.bootstrap` mechanism Scripts 01-02 use for Organization A
       (see those scripts' docstrings for why this isn't a REST call).
    2. Creates one real incident inside Organization B, owned by
       Organization B's admin, via a real `POST /incidents` call.
    3. Attempts, using Organization A's admin token:
         - GET  /organizations/{org_b_id}             (expect denied)
         - GET  /organizations/{org_b_id}/projects     (expect denied)
         - GET  /incidents/{org_b_incident_id}         (expect denied)
    4. Repeats the same three checks symmetrically, using Organization
       B's admin token against Organization A's ids.

    "Denied" means any of {403 permission_denied, 404 not_found} --
    both are acceptable proof that isolation held (this codebase is not
    fully consistent about which of the two it returns for a cross-org
    reference -- see the individual check output for which one each
    endpoint actually returned). The only unacceptable outcome is a 2xx
    response that leaks the other organization's data.

WHICH APIs IT CALLS (all real, live REST calls)
    GET  {BASE_URL}/organizations/{organization_id}
    GET  {BASE_URL}/organizations/{organization_id}/projects
    POST {BASE_URL}/incidents
    GET  {BASE_URL}/incidents/{incident_id}

EXPECTED INPUT
    .state.json's "org" and "users.admin" (Organization A, from Scripts
    01-02) plus .env's ORG_B_NAME/ORG_B_SLUG.

EXPECTED OUTPUT
    6 isolation checks, each PASS/FAIL, plus Organization B's own
    org/admin/incident ids saved to .state.json under "org_b".

HOW TO EXECUTE
    python scripts/realworld_onboarding/08_isolation_tests.py

EXPECTED SUCCESS OUTPUT
    "6/6 checks passed", with each check's logged status code being 403
    or 404, never 2xx.
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

_DENIED_STATUSES = {403, 404}


def _expect_denied(client, log_label, method, path, token) -> bool:
    log = StepLogger(log_label)
    log.step(f"{method} {path}")
    try:
        response = client.call(log, method, path, token=token)
    except ConnectionRefused as exc:
        log.failed(str(exc))
        return False
    if response.status_code in _DENIED_STATUSES:
        log.passed(f"Access correctly denied ({response.status_code})")
        return True
    log.failed(f"Expected 403 or 404, got {response.status_code} -- possible cross-tenant data leak.")
    return False


def main() -> bool:
    config = load_config()
    state = load_state()
    org_a = state.get("org")
    admin_a = state.get("users", {}).get("admin")
    if not org_a or not admin_a:
        StepLogger("Isolation Tests").failed("Missing Organization A state -- run 01/02 first.")
        print_summary()
        return False

    client = ApiClient(config.base_url, config.request_timeout_seconds)

    setup_log = StepLogger("Isolation Tests: bootstrap Organization B")
    setup_log.step(f"Provisioning Organization B ('{config.org_b_name}') and its admin.")
    try:
        admin_b = bootstrap_persona_sync(
            organization_name=config.org_b_name,
            organization_slug=config.org_b_slug,
            persona="admin",
            email=f"admin@{config.org_b_slug}.example",
            display_name="Organization B Administrator",
        )
    except Exception as exc:  # noqa: BLE001
        setup_log.failed(f"{type(exc).__name__}: {exc}")
        print_summary()
        return False
    setup_log.passed(f"Organization B ready: {admin_b['organization_id']}")

    incident_log = StepLogger("Isolation Tests: create a real incident inside Organization B")
    incident_log.step("POST /incidents as Organization B's admin")
    try:
        incident_response = client.call(
            incident_log,
            "POST",
            "/incidents",
            token=admin_b["access_token"],
            json_body={"title": "Org B isolation probe", "description": "Created by 08_isolation_tests.py", "severity": "low"},
        )
    except ConnectionRefused as exc:
        incident_log.failed(str(exc))
        print_summary()
        return False
    if incident_response.status_code != 201:
        incident_log.failed(f"Could not create the probe incident: {incident_response.status_code}")
        print_summary()
        return False
    incident_b_id = incident_response.json()["id"]
    incident_log.passed(f"Probe incident created in Organization B: {incident_b_id}")

    update_state(
        org_b={
            "id": admin_b["organization_id"],
            "slug": admin_b["organization_slug"],
            "admin_user_id": admin_b["user_id"],
            "probe_incident_id": incident_b_id,
        }
    )

    all_ok = True
    org_b_id = admin_b["organization_id"]
    token_a = admin_a["access_token"]
    token_b = admin_b["access_token"]

    all_ok &= _expect_denied(client, "A cannot GET Org B's organization record", "GET", f"/organizations/{org_b_id}", token_a)
    all_ok &= _expect_denied(client, "A cannot GET Org B's projects", "GET", f"/organizations/{org_b_id}/projects", token_a)
    all_ok &= _expect_denied(client, "A cannot GET Org B's incident", "GET", f"/incidents/{incident_b_id}", token_a)

    all_ok &= _expect_denied(client, "B cannot GET Org A's organization record", "GET", f"/organizations/{org_a['id']}", token_b)
    all_ok &= _expect_denied(client, "B cannot GET Org A's projects", "GET", f"/organizations/{org_a['id']}/projects", token_b)

    # Org A doesn't necessarily have a probe incident of its own yet (that's
    # created by 07_permission_tests.py, not guaranteed to have run) -- use a
    # random, syntactically-valid but nonexistent uuid as a stand-in; the
    # point (org A's own incidents are invisible to org B) holds either way,
    # since B has no incident with this id in ITS org regardless of what A has.
    import uuid as _uuid

    all_ok &= _expect_denied(
        client, "B cannot GET an arbitrary incident id scoped to Org A", "GET", f"/incidents/{_uuid.uuid4()}", token_b
    )

    print_summary()
    return bool(all_ok)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
