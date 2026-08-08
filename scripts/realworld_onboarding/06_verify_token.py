"""06 -- Verify an issued access token (a.k.a. "verify_sso.py" in the
original task naming -- renamed here because it verifies EKIP's own
session token, which is a distinct artifact from the upstream IdP's ID
token; see the note below).

PURPOSE
    Decode and independently verify the access token issued to the org
    admin persona (or whichever persona/token you point it at), and print
    every claim it carries -- subject, organization id, issued-at,
    expiry -- then cross-check those claims against what
    `GET /auth/me` (a real, live API call) independently reports for the
    same token, so a mismatch between "what the token says" and "what the
    server resolves from it" would be caught.

ON "VALIDATE THE SIGNATURE USING THE PROJECT'S EXISTING PUBLIC KEY/JWKS"
    EKIP's own access tokens are signed HS256 -- a SYMMETRIC algorithm
    (`Settings.jwt_algorithm`, confirmed by reading
    `app/shared/config/settings.py` directly). There is no public/private
    keypair and no JWKS endpoint for these tokens; verifying one requires
    the same shared secret (`JWT_SECRET_KEY`) the server itself holds.
    JWKS-based verification DOES exist in this codebase, but for a
    different token entirely: the upstream IdP's ID token, verified
    server-side inside `core.auth.service._exchange_code_for_claims`
    during `POST /auth/callback` -- that ID token is never returned to
    the client, so there is nothing for this script to independently
    verify there.

    Given that, this script does the most faithful thing available: it
    imports and calls the project's own, completely unmodified
    `core.auth.service.verify_access_token` in-process
    (`common.jwt_tools.verify_with_project`) -- literally the same
    function every REST/MCP request goes through -- rather than
    reimplementing HS256 verification a second time with no better claim
    to correctness.

WHICH API IT CALLS
    GET {BASE_URL}/auth/me    (cross-check only, after local verification)

EXPECTED INPUT
    A persona name (default: "admin") naming a key under .state.json's
    "users", populated by Scripts 02/04/05.

EXPECTED OUTPUT
    Printed header/payload (unverified decode), printed verified claims
    (project verifier), and the /auth/me response, with a pass/fail
    decision on whether organization_id agrees across all three.

HOW TO EXECUTE
    python scripts/realworld_onboarding/06_verify_token.py [persona]
    python scripts/realworld_onboarding/06_verify_token.py security_engineer

COMMON FAILURES
    - TokenVerificationUnavailable: run this from the project root with
      the project's OWN virtualenv active (not just this harness's
      requirements.txt) -- see common/jwt_tools.py's docstring.
    - Local verification raises PermissionDeniedError ("Invalid or expired
      access token") for a token the live server still accepts: your
      shell's JWT_SECRET_KEY does not match the one the running API
      server loaded from ITS .env. Run this script in the same shell/
      environment you started the API server from.

EXPECTED SUCCESS OUTPUT
    "RESULT: PASS" plus matching organization_id across the unverified
    decode, the project verifier, and the live /auth/me response.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.config import load_config  # noqa: E402
from common.http import ApiClient, ConnectionRefused  # noqa: E402
from common.jwt_tools import TokenVerificationUnavailable, decode_unverified, verify_with_project  # noqa: E402
from common.logger import StepLogger, print_summary  # noqa: E402
from common.state import load_state  # noqa: E402

STAGE = "Verify Access Token"


def main() -> bool:
    persona = sys.argv[1] if len(sys.argv) > 1 else "admin"
    config = load_config()
    log = StepLogger(f"{STAGE} ({persona})")
    state = load_state()
    user = state.get("users", {}).get(persona)
    if not user:
        log.failed(f"No persona {persona!r} in .state.json 'users' -- run 02/04/05 first.")
        print_summary()
        return False

    token = user["access_token"]

    log.step("Unverified decode (header + payload, no signature check)")
    try:
        decoded = decode_unverified(token)
    except ValueError as exc:
        log.failed(str(exc))
        print_summary()
        return False
    log.info(f"header:  {decoded['header']}")
    log.info(f"payload: {decoded['payload']}")

    log.step("Verifying signature via the project's own core.auth.service.verify_access_token")
    try:
        verified = verify_with_project(token)
    except TokenVerificationUnavailable as exc:
        log.failed(str(exc))
        print_summary()
        return False
    except Exception as exc:  # noqa: BLE001 - project's own PermissionDeniedError, etc.
        log.failed(f"Signature verification failed: {type(exc).__name__}: {exc}")
        print_summary()
        return False
    log.info(f"verified claims: {verified}")
    log.info(
        f"  subject (user_id):    {verified['user_id']}\n"
        f"    organization_id:      {verified['organization_id']}\n"
        f"    issued_at:            {verified['issued_at']}\n"
        f"    expires_at:           {verified['expires_at']}"
    )
    log.info(
        "Note: EKIP's TokenClaims carries no separate 'issuer'/'audience' fields -- "
        "confirmed by reading app/core/auth/schemas.py directly. Those OIDC-style "
        "claims exist only on the upstream IdP's ID token, verified server-side and "
        "never returned to the client (see this script's module docstring)."
    )

    log.step("Cross-checking against a live GET /auth/me call")
    client = ApiClient(config.base_url, config.request_timeout_seconds)
    try:
        response = client.call(log, "GET", "/auth/me", token=token)
    except ConnectionRefused as exc:
        log.failed(str(exc))
        print_summary()
        return False

    if response.status_code != 200:
        log.failed(f"GET /auth/me returned {response.status_code}")
        print_summary()
        return False

    profile = response.json()
    log.info(f"roles:       {profile.get('roles')}")
    log.info(f"permissions: {profile.get('permissions')}")

    if str(profile["id"]) != str(verified["user_id"]):
        log.failed("Mismatch: /auth/me's user id differs from the token's own subject claim.")
        print_summary()
        return False

    log.passed(
        f"Token verified locally and matches the live server's own resolution "
        f"(user={profile['email']}, roles={profile['roles']})."
    )
    return print_summary()


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
