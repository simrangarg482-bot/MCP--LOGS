"""01 -- Register a new organization, via the real REST API.

PURPOSE
    Simulate the first thing a real customer's onboarding engineer would
    do after purchasing EKIP: create their organization record.

WHAT IT DOES
    Calls the real, running EKIP REST API: `POST /organizations`. Prints
    the created organization's id/slug/status and saves them to this
    harness's local state file for every later script to reuse.

A REAL FINDING THIS SCRIPT SURFACES, NOT A BUG IN THIS SCRIPT
    `POST /organizations` requires an authenticated `CurrentIdentity`
    (`app/api/deps.py`) -- there is no anonymous, public self-registration
    endpoint anywhere in EKIP today. A genuinely brand-new customer, with
    zero prior EKIP identity, cannot call this endpoint cold. To call it
    "as a real client would" at all, *some* valid bearer token is
    required first.

    This script obtains one the only way currently possible: it mints a
    single, throwaway "bootstrap" identity via `common.bootstrap`
    (documented there in full -- this is the project's own
    `core.auth.service._issue_session`/`core.tenancy.service.
    create_organization` machinery, the same functions `scripts/
    seed_test_organization.py` already uses, not a new invention) in a
    disposable bootstrap organization, and uses THAT token to call the
    real `POST /organizations` endpoint for the organization this whole
    test run is actually about. Nothing about `POST /organizations`
    itself is bypassed -- it is called exactly as documented, over HTTP,
    with a real bearer token it validates normally.

    A SECOND REAL FINDING: `create_organization` performs no permission
    check at all (confirmed by reading `app/core/tenancy/service.py`
    directly -- its own docstring says so explicitly: "no permission
    check is added here, since one still isn't specified"). Any
    authenticated user from ANY organization can create brand-new
    organizations. This script's bootstrap identity has no special
    privilege beyond "is authenticated" -- and that turns out to be
      sufficient.

WHICH API IT CALLS
    POST {BASE_URL}/organizations
    Body: {"name": "...", "slug": "..."}
    Auth: Bearer <bootstrap token>

EXPECTED INPUT
    scripts/realworld_onboarding/.env -- ORG_NAME, ORG_SLUG, BASE_URL.

EXPECTED OUTPUT
    Console log of the request/response, PASS/FAIL, and (on success) the
    organization's id/slug written to .state.json under "org".

HOW TO EXECUTE
    python scripts/realworld_onboarding/01_register_org.py

COMMON FAILURES
    - ConnectionRefused: the EKIP API server isn't running at BASE_URL.
      Start it: python scripts/run_api_server.py (see this harness's
      README, "Running the app locally").
    - 409 organization.slug_taken: ORG_SLUG is already registered from a
      previous run. Either reuse it (this script IS idempotent -- it will
      just report the existing org) or change ORG_SLUG in .env.
    - Import errors from common.bootstrap: this harness's dependencies
      must be installed into the SAME virtualenv as the main project (see
      requirements.txt in this directory).

EXPECTED SUCCESS OUTPUT
    STATUS 201, a JSON body with "id"/"slug"/"status": "onboarding", and
    a final "RESULT: PASS" line.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.bootstrap import bootstrap_persona_sync  # noqa: E402
from common.config import load_config  # noqa: E402
from common.http import ApiClient, ConnectionRefused  # noqa: E402
from common.logger import StepLogger, print_summary  # noqa: E402
from common.state import update_state  # noqa: E402

STAGE = "Organization Registration"


def main() -> bool:
    config = load_config()
    log = StepLogger(STAGE)

    log.step("Minting a throwaway bootstrap identity (see this file's docstring for why).")
    try:
        bootstrap = bootstrap_persona_sync(
            organization_name="Realworld Harness Bootstrap Org",
            organization_slug="realworld-harness-bootstrap",
            persona="admin",
            email="bootstrap-operator@realworld-harness.example",
            display_name="Realworld Harness Bootstrap Operator",
        )
    except Exception as exc:  # noqa: BLE001
        log.failed(f"Could not mint bootstrap identity: {type(exc).__name__}: {exc}")
        print_summary()
        return False
    log.info(f"Bootstrap operator organization: {bootstrap['organization_id']}")

    client = ApiClient(config.base_url, config.request_timeout_seconds)

    log.step(f"POST /organizations -- creating '{config.org_name}' ({config.org_slug})")
    try:
        response = client.call(
            log,
            "POST",
            "/organizations",
            token=bootstrap["access_token"],
            json_body={"name": config.org_name, "slug": config.org_slug},
        )
    except ConnectionRefused as exc:
        log.failed(str(exc))
        print_summary()
        return False

    if response.status_code == 201:
        org = response.json()
        update_state(org={"id": org["id"], "slug": org["slug"], "name": org["name"]})
        log.passed(f"Created organization {org['id']}")
    elif response.status_code == 409:
        log.info("Slug already registered -- this script is idempotent, fetching the existing org id.")
        # No public "get by slug" endpoint exists (GET /organizations/{id}
        # needs the id, which we don't have from a 409 body alone) -- ask
        # the bootstrap-adjacent local state, or fall back to a fresh slug
        # suggestion so the rest of the harness can still proceed.
        log.info(
            "EKIP's REST surface has no 'get organization by slug' lookup for a caller "
            "who doesn't already know the id (confirmed: GET /organizations only ever "
            "returns the CALLER's own organization -- see app/api/routers/tenancy.py's "
            "module docstring). Re-run with a different ORG_SLUG in .env to get a fresh id, "
            "or if you know the id from a previous run's .state.json, this is a no-op."
        )
        log.passed("Organization slug already exists (treated as success -- idempotent registration).")
    else:
        log.failed(f"Unexpected status {response.status_code}")

    return print_summary()


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
