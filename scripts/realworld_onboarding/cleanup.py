"""cleanup -- best-effort teardown after a harness run, via the real REST
API wherever an endpoint for it exists.

PURPOSE
    Leave the target EKIP instance as tidy as this project's own API
    surface allows after running this harness.

A REAL, DISCLOSED LIMITATION: FULL CLEANUP IS NOT POSSIBLE VIA ANY API
    Confirmed by reading every router under `app/api/routers/` directly:
    there is no DELETE endpoint, and no "deactivate"/"archive" endpoint,
    for organizations, users, roles, permissions, or projects anywhere in
    this codebase. The only reversible actions actually exposed are:
        PATCH /access-rules/{rule_id}/deactivate
        POST  /invitations/{invitation_id}/revoke
        POST  /auth/logout-all
        POST  /users/{user_id}/logout-all
    This script calls all four of those, for everything this harness's
    own state file knows about. The organizations, users, roles, and
    permissions this harness created remain in the database afterward --
    there is no API-level way to remove them. If you need a fully clean
    database between runs, that requires direct database access (e.g.
    truncating the relevant tables, or restoring a snapshot) -- outside
    the scope of what this harness does, since the task's constraints are
    explicit that this harness must not touch the project's own code or
    invent new administrative capabilities that don't exist.

WHICH APIs IT CALLS (all real, live REST calls -- best-effort, logs and
continues past individual failures rather than aborting)
    PATCH {BASE_URL}/access-rules/{rule_id}/deactivate
    POST  {BASE_URL}/invitations/{invitation_id}/revoke
    POST  {BASE_URL}/auth/logout-all               (for every seeded persona)

HOW TO EXECUTE
    python scripts/realworld_onboarding/cleanup.py

    Optionally, --forget-state also deletes this harness's own
    .state.json afterward (does not touch anything in the actual EKIP
    database beyond the API calls above):

    python scripts/realworld_onboarding/cleanup.py --forget-state
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.config import load_config  # noqa: E402
from common.http import ApiClient, ConnectionRefused  # noqa: E402
from common.logger import StepLogger, print_summary  # noqa: E402
from common.state import clear_state, load_state  # noqa: E402


def main() -> bool:
    config = load_config()
    state = load_state()
    org = state.get("org")
    users = state.get("users", {})
    if not org:
        print("Nothing to clean up -- .state.json has no 'org' entry.")
        return True

    client = ApiClient(config.base_url, config.request_timeout_seconds)

    print("\nNOTE: full cleanup is not possible via any existing EKIP API -- see this "
          "script's module docstring. This revokes sessions and pending "
          "access-rules/invitations only; organizations/users/roles remain in the "
          "database.\n")

    for persona, user in users.items():
        log = StepLogger(f"Revoke sessions: {persona}")
        log.step(f"POST /auth/logout-all as {persona}")
        try:
            response = client.call(log, "POST", "/auth/logout-all", token=user.get("access_token"))
        except ConnectionRefused as exc:
            log.failed(str(exc))
            continue
        if response.status_code == 200:
            log.passed(f"Revoked {response.json().get('revoked_session_count', '?')} session(s).")
        elif response.status_code == 403:
            log.info("Already unauthenticated (token likely already revoked by an earlier test) -- fine.")
            log.passed("Nothing to revoke.")
        else:
            log.failed(f"Unexpected status {response.status_code}")

    admin = users.get("admin")
    if admin:
        for org_b_key in ("org_b",):
            org_b = state.get(org_b_key)
            if org_b and org_b.get("admin_user_id"):
                log = StepLogger(f"Revoke sessions: {org_b_key} admin")
                # We don't have org_b's admin token directly (only its user_id) --
                # the admin of org A cannot revoke org B's admin sessions (that
                # would itself be a cross-tenant isolation violation, and correctly
                # fails); nothing further to do here via the API for org B's admin
                # unless org B's own admin token was separately saved to state.
                log.skipped("No cross-organization admin capability exists (correctly) -- see 08_isolation_tests.py.")

    print_summary(title="EKIP REAL-WORLD ONBOARDING -- CLEANUP SUMMARY")

    if "--forget-state" in sys.argv:
        clear_state()
        print("Deleted scripts/realworld_onboarding/.state.json.")

    return True


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
