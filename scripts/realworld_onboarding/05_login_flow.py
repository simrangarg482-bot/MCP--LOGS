"""05 -- Real SSO login: Authorization Code + PKCE, against a REAL Identity
Provider.

PURPOSE
    The one script in this harness that actually exercises the live,
    end-to-end OIDC handshake: `begin_sso_login` -> employee's browser at
    the real IdP -> IdP redirects back with a code -> `complete_sso_login`
    exchanges it for a real EKIP session, including verifying the IdP's
    ID token signature against its own JWKS.

WHICH APIs IT CALLS
    GET  {BASE_URL}/auth/{org_slug}/login?redirect_uri=...
         -> {authorization_url, state, code_verifier}
    (interactive step -- see below)
    POST {BASE_URL}/auth/callback?redirect_uri=...
         Body: {"org_slug", "code", "state", "code_verifier"}
         -> {access_token, refresh_token, token_type, expires_in}

REQUIRES REAL CREDENTIALS -- THIS CANNOT BE FAKED
    Unlike every other script in this harness, this one has no
    non-API fallback, because there is nothing to fall back to: an
    interactive human login at a real Identity Provider is the entire
    thing being tested. If `.env`'s CLIENT_ID / CLIENT_SECRET / ISSUER
    are not all set to a real, registered OIDC application at one of
    EKIP's four already-supported providers (Microsoft Entra ID, Okta,
    Auth0, Google Workspace), this script prints exactly what is missing
    and exits without attempting a fake login. This is a genuine,
    disclosed limitation of what can be automated in an environment with
    no real IdP tenant -- not a bug in this script.

WHAT "INTERACTIVE" MEANS HERE
    This script prints the real `authorization_url` and asks you to open
    it, complete a normal login at your IdP (username, password, MFA if
    your org requires it), and paste back the FULL URL your browser lands
    on afterward (it will look like
    `{REDIRECT_URI}?code=...&state=...`). This script then extracts
    `code` from that URL itself -- you never need to parse it by hand.

    If you are running this from inside an agent session that has browser
    automation available (e.g. Claude in Chrome), you can instead have
    the agent navigate to the printed `authorization_url`, complete the
    login using TEST_EMAIL/TEST_PASSWORD from `.env`, and read the
    resulting redirect URL from the browser's address bar -- then paste
    that URL at this script's prompt exactly as if you had done it by
    hand. This script does not launch a browser itself (no Selenium/
    Playwright dependency is added -- see this harness's requirements.txt
    and the constraint against adding new dependencies/behavior beyond
    what the task called for).

A NOTE FROM READING core/auth/service.py DIRECTLY
    Its own module docstring states plainly: "`_discover_authorization_
    endpoint` and `_exchange_code_for_claims` are now real implementations
    ... This has NOT been run against a live IdP ... treat this as
    spec-correct-by-inspection, not battle-tested, until it's actually
    exercised against a real provider." Running this script for the first
    time against a real IdP is, as far as this codebase's own commit
    history is concerned, the first real exercise of that code path.

EXPECTED INPUT
    .env's SSO_PROVIDER/CLIENT_ID/CLIENT_SECRET/ISSUER/REDIRECT_URI
    (must exactly match a real OIDC app registration, including
    REDIRECT_URI being registered as an allowed redirect at the IdP), plus
    .state.json's "org"/"sso" (Scripts 01/03 -- the SSO configuration
    submitted in Script 03 must be the REAL issuer/client, not the fake
    placeholder values used when no real IdP was available).

EXPECTED OUTPUT
    A real SessionTokens triple, saved to .state.json under
    "users.sso_login".

HOW TO EXECUTE
    python scripts/realworld_onboarding/05_login_flow.py

COMMON FAILURES
    - "Skipped: no real IdP credentials configured": expected when .env's
      CLIENT_ID/CLIENT_SECRET/ISSUER are blank. Fill them in with a real
      OIDC app to exercise this script.
    - httpx.HTTPStatusError fetching the discovery document: ISSUER is
      wrong, or the IdP's `.well-known/openid-configuration` isn't
      reachable from this machine (corporate network/proxy).
    - auth.idp_response_invalid / auth.idp_key_not_found /
      auth.idp_token_invalid (403 from POST /auth/callback): the IdP
      returned an ID token this server couldn't verify -- check
      REDIRECT_URI matches exactly what's registered at the IdP (OAuth2
      requires byte-for-byte equality), and that CLIENT_SECRET is correct
      (Script 03's note: it is currently stored/read back as plaintext,
      so a copy-paste error here is the most common cause).
    - "state mismatch": you pasted a stale/reused redirect URL from a
      previous attempt. Re-run the script for a fresh `state`.

EXPECTED SUCCESS OUTPUT
    STATUS 200 from POST /auth/callback with "access_token"/
    "refresh_token" in the body, and "RESULT: PASS".
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.config import load_config  # noqa: E402
from common.http import ApiClient, ConnectionRefused  # noqa: E402
from common.logger import StepLogger, print_summary  # noqa: E402
from common.state import load_state, update_state  # noqa: E402

STAGE = "Real SSO Login (Authorization Code + PKCE)"


def main() -> bool:
    config = load_config()
    log = StepLogger(STAGE)
    state = load_state()
    org = state.get("org")

    if not config.has_real_idp_credentials:
        log.skipped(
            "No real IdP credentials in .env (CLIENT_ID/CLIENT_SECRET/ISSUER are blank). "
            "This is the one scenario this harness genuinely cannot fake -- see this "
            "file's module docstring. Every downstream script (06-10) instead uses the "
            "real, normally-signed sessions seeded by common.bootstrap in Scripts 02/04, "
            "which every EKIP endpoint accepts identically to a token from a real login."
        )
        print_summary()
        return True  # a documented skip, not a failure

    if not org:
        log.failed("Missing .state.json 'org' -- run 01_register_org.py first.")
        print_summary()
        return False

    client = ApiClient(config.base_url, config.request_timeout_seconds)

    log.step(f"GET /auth/{org['slug']}/login?redirect_uri={config.redirect_uri}")
    try:
        begin_response = client.call(
            log, "GET", f"/auth/{org['slug']}/login", params={"redirect_uri": config.redirect_uri}
        )
    except ConnectionRefused as exc:
        log.failed(str(exc))
        print_summary()
        return False

    if begin_response.status_code != 200:
        log.failed(f"begin_sso_login failed with status {begin_response.status_code}")
        print_summary()
        return False

    redirect = begin_response.json()
    authorization_url = redirect["authorization_url"]
    expected_state = redirect["state"]
    code_verifier = redirect["code_verifier"]

    print("\n" + "=" * 64)
    print("ACTION REQUIRED -- complete a real login in your browser:")
    print("=" * 64)
    print(f"\n1. Open this URL:\n\n   {authorization_url}\n")
    print("2. Log in with your real IdP test user (TEST_EMAIL from .env, if set).")
    print("3. After login, your browser will land on a URL starting with:")
    print(f"   {config.redirect_uri}?code=...&state=...")
    print("   (this will 404 or show a blank page -- that's expected, nothing is")
    print("   listening on that redirect URI in this harness; copy the URL from")
    print("   the browser's address bar anyway.)\n")
    pasted_url = input("Paste the full redirect URL here, then press Enter: ").strip()

    parsed = urlparse(pasted_url)
    query = parse_qs(parsed.query)
    code = (query.get("code") or [None])[0]
    returned_state = (query.get("state") or [None])[0]

    if not code:
        log.failed("No 'code' parameter found in the pasted URL.")
        print_summary()
        return False
    if returned_state != expected_state:
        log.failed(
            f"State mismatch: expected {expected_state!r}, got {returned_state!r}. "
            "This is the real anti-CSRF check every OAuth2 client must perform -- "
            "re-run this script for a fresh authorization_url/state pair."
        )
        print_summary()
        return False
    log.info("state matched -- proceeding to token exchange.")

    log.step("POST /auth/callback -- exchanging the code for a real EKIP session")
    try:
        callback_response = client.call(
            log,
            "POST",
            "/auth/callback",
            params={"redirect_uri": config.redirect_uri},
            json_body={
                "org_slug": org["slug"],
                "code": code,
                "state": returned_state,
                "code_verifier": code_verifier,
            },
        )
    except ConnectionRefused as exc:
        log.failed(str(exc))
        print_summary()
        return False

    if callback_response.status_code != 200:
        log.failed(f"complete_sso_login failed with status {callback_response.status_code}")
        print_summary()
        return False

    tokens = callback_response.json()
    users = load_state().get("users", {})
    users["sso_login"] = {
        "organization_id": org["id"],
        "organization_slug": org["slug"],
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_in": tokens["expires_in"],
    }
    update_state(users=users)
    log.passed("Real SSO login completed successfully -- a genuine, IdP-verified session issued.")
    return print_summary()


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
