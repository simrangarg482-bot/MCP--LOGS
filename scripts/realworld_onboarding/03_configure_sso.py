"""03 -- Configure SSO for the organization, via the real REST API.

PURPOSE
    Simulate an org administrator connecting their company's Identity
    Provider (Microsoft Entra ID, Okta, Auth0, or Google Workspace --
    whichever four EKIP already supports; this script adds none) to
    EKIP.

WHICH API IT CALLS
    POST {BASE_URL}/organizations/{organization_id}/sso/configure
    Body: {"provider", "protocol", "issuer_url", "client_id", "client_secret_ref"}
    Auth: Bearer <org admin token, from 02_bootstrap_org_admin.py>

    This is a real, unmodified endpoint added to this project's tenancy
    admin surface -- `app/api/routers/tenancy.py`'s `admin_router`,
    calling straight into `core.tenancy.service.configure_sso`, which
    requires `tenancy:manage` (the admin persona from Script 02 holds it)
    and raises ConflictError if SSO is already configured for this
    organization (not an upsert) -- this script treats a 409 here as an
    idempotent success, not a failure.

A REAL FINDING THIS SCRIPT SURFACES
    `configure_sso`'s request body field is literally named
    `client_secret_ref` and its own schema docstring
    (`app/core/tenancy/schemas.py`) says it is expected to already be "a
    valid reference into the encrypted secret store". In practice, no
    such secret store exists for SSO client secrets: reading
    `core.auth.service._resolve_client_secret` directly shows it treats
    whatever was submitted as the literal plaintext secret, unchanged --
    unlike connector credentials (`ConnectorConfigCreate.credential_ref`),
    which Milestone 10 wired through real AES-256-GCM envelope encryption
    (`app.shared.security`), `SSOConfigurationCreate.client_secret_ref`
    was never included in that pass. Practically: this script submits the
    real client secret directly in this field, and it is stored in
    `sso_configurations.client_secret_ref` as plaintext. This is a real,
    unaddressed gap worth flagging to the project owner -- not something
    this harness works around or fixes.

WHEN NO REAL IDP CREDENTIALS ARE CONFIGURED (.env's CLIENT_ID/
CLIENT_SECRET/ISSUER left blank)
    This script still exercises the configuration API itself with
    clearly-fake placeholder values (`issuer_url=https://fake-idp.invalid/
    ...`) so Scripts 06-10 (permission/isolation/negative/logout testing,
    all of which only need EKIP's OWN tokens, not a real IdP round trip)
    still have a configured SSO record to reference. Script 05
    (login_flow) is the only script that actually requires real IdP
    reachability, and it checks for that itself.

EXPECTED INPUT
    .state.json's "org" and "users"."admin" keys (from Scripts 01-02).

EXPECTED OUTPUT
    The created (or already-existing) SSOConfiguration, logged in full.

HOW TO EXECUTE
    python scripts/realworld_onboarding/03_configure_sso.py

COMMON FAILURES
    - 403 permission_denied: the admin token's role doesn't grant
      `tenancy:manage` -- re-run 02_bootstrap_org_admin.py first.
    - 409 conflict (organization already has SSO configured): expected
      and idempotent on a second run of this whole harness against the
      same organization; treated as PASS.
    - 422 validation error on `provider`: SSO_PROVIDER in .env must be
      exactly one of entra_id | okta | auth0 | google_workspace.

EXPECTED SUCCESS OUTPUT
    STATUS 201 (or a 409 treated as PASS), body containing "provider",
    "issuer_url", "client_id".
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.config import load_config  # noqa: E402
from common.http import ApiClient, ConnectionRefused  # noqa: E402
from common.logger import StepLogger, print_summary  # noqa: E402
from common.state import load_state, update_state  # noqa: E402

STAGE = "Configure SSO"
_VALID_PROVIDERS = {"entra_id", "okta", "auth0", "google_workspace"}


def main() -> bool:
    config = load_config()
    log = StepLogger(STAGE)
    state = load_state()

    org = state.get("org")
    admin = state.get("users", {}).get("admin")
    if not org or not admin:
        log.failed("Missing .state.json 'org'/'users.admin' -- run 01_register_org.py and "
                   "02_bootstrap_org_admin.py first.")
        print_summary()
        return False

    provider = config.sso_provider if config.sso_provider in _VALID_PROVIDERS else "okta"
    if config.has_real_idp_credentials:
        log.info(f"Using REAL IdP credentials from .env for provider={provider!r}.")
        issuer_url = config.issuer
        client_id = config.client_id
        client_secret_ref = config.client_secret
    else:
        log.warn(
            "No real IdP credentials in .env (CLIENT_ID/CLIENT_SECRET/ISSUER) -- "
            "configuring SSO with clearly-fake placeholder values so the "
            "configuration API itself can still be exercised. Script 05 "
            "(login_flow) will not be able to complete a real login against this."
        )
        issuer_url = f"https://fake-idp.invalid/{org['slug']}"
        client_id = f"fake-client-id-{org['slug']}"
        client_secret_ref = "fake-client-secret-placeholder"

    client = ApiClient(config.base_url, config.request_timeout_seconds)
    log.step(f"POST /organizations/{org['id']}/sso/configure")
    try:
        response = client.call(
            log,
            "POST",
            f"/organizations/{org['id']}/sso/configure",
            token=admin["access_token"],
            json_body={
                "provider": provider,
                "protocol": "oidc",
                "issuer_url": issuer_url,
                "client_id": client_id,
                "client_secret_ref": client_secret_ref,
            },
        )
    except ConnectionRefused as exc:
        log.failed(str(exc))
        print_summary()
        return False

    if response.status_code == 201:
        sso = response.json()
        update_state(sso={"organization_id": org["id"], "provider": sso["provider"], "issuer_url": sso["issuer_url"]})
        log.passed(f"SSO configured: provider={sso['provider']}")
    elif response.status_code == 409:
        log.info("SSO already configured for this organization -- idempotent, treated as success.")
        update_state(sso={"organization_id": org["id"], "provider": provider, "issuer_url": issuer_url})
        log.passed("SSO configuration already existed.")
    else:
        log.failed(f"Unexpected status {response.status_code}")

    return print_summary()


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
