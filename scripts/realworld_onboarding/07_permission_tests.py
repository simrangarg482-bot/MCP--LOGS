"""07 -- Permission testing: allowed requests, forbidden requests, missing
permission, across all five personas.

PURPOSE
    Prove role-based access control actually works end-to-end through the
    real REST API, for real tokens belonging to real (seeded) users, not
    just at the unit-test level.

METHODOLOGY
    Four endpoints were chosen specifically because each is gated by a
    permission check that runs with no prior "does this resource exist"
    lookup (confirmed by reading each one's `core/*/service.py` directly)
    -- so a call against any of them deterministically returns either the
    success status or 403 permission_denied, with no ambiguity from an
    unrelated 404. (`POST /postmortems/{id}/approve` was deliberately
    excluded from this matrix: reading `core.incidents.service.
    approve_postmortem` shows it fetches the postmortem row BEFORE
    checking `postmortem:approve`, so a nonexistent id always 404s
    regardless of role -- not a clean permission-only signal.)

    Expected allow/deny matrix (from `common/bootstrap.py`'s `PERSONAS`,
    the same role/permission grants Script 02/04 actually assigned):

                        incident   observability  knowledge   access-rule
                        :write     :read          :review     (tenancy:manage)
      admin               ALLOW      ALLOW          ALLOW        ALLOW
      security_engineer   ALLOW      ALLOW          DENY         DENY
      developer           ALLOW      DENY           DENY         DENY
      manager             DENY       ALLOW          ALLOW        DENY
      read_only           DENY       ALLOW          DENY         DENY

WHICH APIs IT CALLS (all real, live REST calls)
    POST {BASE_URL}/incidents
    GET  {BASE_URL}/observability/agents
    GET  {BASE_URL}/knowledge/proposed
    POST {BASE_URL}/organizations/{organization_id}/access-rules

EXPECTED INPUT
    .state.json's "org" and "users" (all five personas -- run
    02_bootstrap_org_admin.py and 04_create_test_users.py first).

EXPECTED OUTPUT
    20 checks (5 personas x 4 endpoints), each logged PASS/FAIL against
    the matrix above.

HOW TO EXECUTE
    python scripts/realworld_onboarding/07_permission_tests.py

COMMON FAILURES
    - Every check fails with 403: a persona's role wasn't actually
      assigned -- re-run 02/04, or check .state.json's "users" entries
      each have a distinct, real "role_id".
    - "missing persona": run 04_create_test_users.py first.

EXPECTED SUCCESS OUTPUT
    "20/20 checks passed."
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.config import load_config  # noqa: E402
from common.http import ApiClient, ConnectionRefused  # noqa: E402
from common.logger import StepLogger, print_summary  # noqa: E402
from common.state import load_state  # noqa: E402

# (endpoint label, method, path template, json body or None, success status, allowed personas)
_CHECKS = [
    (
        "incident:write -> POST /incidents",
        "POST",
        "/incidents",
        {"title": "Permission probe incident", "description": "Created by 07_permission_tests.py", "severity": "low"},
        201,
        {"admin", "security_engineer", "developer"},
    ),
    (
        "observability:read -> GET /observability/agents",
        "GET",
        "/observability/agents",
        None,
        200,
        {"admin", "security_engineer", "manager", "read_only"},
    ),
    (
        "knowledge:review -> GET /knowledge/proposed",
        "GET",
        "/knowledge/proposed",
        None,
        200,
        {"admin", "manager"},
    ),
    (
        "tenancy:manage -> POST /organizations/{id}/access-rules",
        "POST",
        "/organizations/{org_id}/access-rules",
        {"rule_type": "domain", "value": "permission-probe.example", "grants_role": "read_only"},
        201,
        {"admin"},
    ),
]

_ALL_PERSONAS = ["admin", "security_engineer", "developer", "manager", "read_only"]


def main() -> bool:
    config = load_config()
    state = load_state()
    org = state.get("org")
    users = state.get("users", {})
    if not org:
        StepLogger("Permission Tests").failed("Missing .state.json 'org' -- run 01_register_org.py first.")
        print_summary()
        return False

    client = ApiClient(config.base_url, config.request_timeout_seconds)
    all_ok = True

    for label, method, path_template, body, success_status, allowed in _CHECKS:
        path = path_template.format(org_id=org["id"])
        for persona in _ALL_PERSONAS:
            log = StepLogger(f"{label} :: {persona}")
            user = users.get(persona)
            if not user:
                log.failed(f"No persona {persona!r} in .state.json -- run 04_create_test_users.py first.")
                all_ok = False
                continue

            log.step(f"{method} {path} as {persona}")
            try:
                response = client.call(log, method, path, token=user["access_token"], json_body=body)
            except ConnectionRefused as exc:
                log.failed(str(exc))
                all_ok = False
                continue

            should_allow = persona in allowed
            if should_allow and response.status_code == success_status:
                log.passed(f"Allowed as expected ({response.status_code})")
            elif not should_allow and response.status_code == 403:
                log.passed("Denied as expected (403 permission_denied)")
            else:
                expectation = f"expected {success_status if should_allow else 403}"
                log.failed(f"{expectation}, got {response.status_code}")
                all_ok = False

    print_summary()
    return all_ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
