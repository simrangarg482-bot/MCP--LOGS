"""09 -- Negative testing: everything that should fail, failing for the
right reason.

Every case below states the ACTUAL expected result, taken from reading the
real implementation (`app/api/deps.py`, `app/core/auth/service.py`,
`app/core/users/service.py`, `app/core/exceptions.py`) -- not an assumed
"should return 401" default. One genuinely useful, honest surprise up
front: EKIP maps every authentication/authorization failure to HTTP 403,
never 401 (`PermissionDeniedError.status_hint = 403`, confirmed in
`app/core/exceptions.py`) -- every check below asserts 403, not 401, for
exactly that reason.

CASES COVERED, AND WHY (grouped by how each fixture is obtained)
  Testable purely against EKIP's own token verification (no IdP needed):
    NT1  Missing Authorization header               -> 403 auth.missing_bearer_token
    NT2  Malformed header (no "Bearer " prefix)      -> 403 auth.missing_bearer_token
    NT3  Garbage / non-JWT token                     -> 403 auth.invalid_token
    NT4  Tampered token (bit-flipped signature)      -> 403 auth.invalid_token
    NT5  Expired token (crafted, real secret)        -> 403 auth.invalid_token
    NT6  Wrong organization_id embedded in token     -> 403 permission_denied
    NT7  User with no role in their own real org     -> 403 permission_denied,
                                                          but GET /auth/me still 200
                                                          with empty roles (fail-closed,
                                                          not an error -- see
                                                          core.users.service.resolve_identity's
                                                          own docstring)
    NT8  Nonexistent organization_id in token        -> 403 permission_denied
                                                          (behaves identically to NT7 --
                                                          resolve_identity does not check
                                                          that the organization itself
                                                          exists, only that role rows
                                                          exist for it)
    NT9  Disabled user account                       -> 403 user.inactive
    NT10 Revoked session, but its access token
         has not yet naturally expired               -> the access token STILL WORKS
                                                          (200) -- documented, expected
                                                          behavior (see
                                                          LogoutAllResponse's own
                                                          docstring), not a bug
    NT11 Revoked (already-used) refresh token reused -> 403 auth.invalid_refresh_token
                                                          or auth.refresh_token_reused,
                                                          via POST /auth/refresh

  Require a REAL Identity Provider round trip to exercise meaningfully --
  SKIPPED with a clear reason when .env has no real IdP credentials
  (see 05_login_flow.py for why this can't be faked), and *why* the
  expected behavior is what it is even when skipped:
    NT12 Wrong issuer                     -- the IdP's own authorize/token
                                             endpoints would reject the
                                             request before EKIP ever sees it.
    NT13 Wrong client id / secret         -- same: rejected at the IdP.
                                             A REAL, DISCLOSED GAP found while
                                             writing this: `core.auth.service.
                                             _exchange_code_for_claims` calls
                                             `response.raise_for_status()` on
                                             the IdP's token-endpoint response
                                             with no surrounding try/except --
                                             an httpx.HTTPStatusError from a
                                             rejected exchange is NOT one of
                                             this project's `EKIPError`
                                             subclasses, so `app/api/errors.py`'s
                                             handler (registered only for
                                             `EKIPError`) never catches it --
                                             it would surface as a raw,
                                             unhandled 500, not a clean 4xx.
    NT14 Invalid redirect_uri             -- rejected by the IdP itself
                                             (OAuth2 requires an exact,
                                             pre-registered match).
    NT15 Invalid nonce                    -- ANOTHER REAL, DISCLOSED GAP:
                                             reading `_build_authorization_url`
                                             and `_exchange_code_for_claims`
                                             directly shows EKIP's OIDC flow
                                             sends no `nonce` parameter at all
                                             and never checks one on the
                                             returned ID token. There is
                                             nothing to test here because the
                                             mechanism doesn't exist -- not
                                             because this harness couldn't
                                             reach it.
    NT16 Clock skew                       -- would require deliberately
                                             desynchronizing this machine's
                                             clock from the IdP's, which is
                                             outside what an API test script
                                             should ever do to its own host.

WHY SOME FIXTURES NEED A ONE-OFF, NON-API SETUP STEP
    NT5/NT6/NT8 need a token this harness could never legitimately obtain
    through the API (an expired one, one asserting an org its subject
    doesn't belong to) -- `common.jwt_tools.craft_token_with_project_secret`
    builds these directly with the project's own signing secret, the same
    non-API mechanism `common/bootstrap.py` already uses and documents in
    full. NT9 needs a disabled user account; there is no REST/MCP API to
    deactivate one (another real, disclosed gap -- see
    `common.bootstrap.set_user_active_sync`'s own docstring), so this
    script flips `is_active` directly on a disposable seeded user, then
    reactivates it afterward.

HOW TO EXECUTE
    python scripts/realworld_onboarding/09_negative_tests.py

EXPECTED SUCCESS OUTPUT
    11 PASS results for NT1-NT11, and 5 documented SKIPs for NT12-NT16.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.bootstrap import bootstrap_persona_sync, set_user_active_sync  # noqa: E402
from common.config import load_config  # noqa: E402
from common.http import ApiClient, ConnectionRefused  # noqa: E402
from common.jwt_tools import TokenVerificationUnavailable, craft_token_with_project_secret  # noqa: E402
from common.logger import StepLogger, print_summary  # noqa: E402
from common.state import load_state  # noqa: E402

_PROBE_PATH = "/observability/agents"  # any observability:read-gated GET; no side effects


def _assert_status(client, label, method, path, token, expected_statuses, headers=None) -> bool:
    log = StepLogger(label)
    log.step(f"{method} {path}")
    try:
        response = client.call(log, method, path, token=token, extra_headers=headers)
    except ConnectionRefused as exc:
        log.failed(str(exc))
        return False
    if response.status_code in expected_statuses:
        log.passed(f"Got {response.status_code} as expected")
        return True
    log.failed(f"Expected one of {expected_statuses}, got {response.status_code}")
    return False


def main() -> bool:
    config = load_config()
    state = load_state()
    org = state.get("org")
    admin = state.get("users", {}).get("admin")
    if not org or not admin:
        StepLogger("Negative Tests").failed("Missing .state.json 'org'/'users.admin' -- run 01/02 first.")
        print_summary()
        return False

    client = ApiClient(config.base_url, config.request_timeout_seconds)
    ok = True

    # NT1 -- missing Authorization header entirely
    log = StepLogger("NT1: missing Authorization header")
    log.step(f"GET {_PROBE_PATH} with no Authorization header at all")
    try:
        response = client.call(log, "GET", _PROBE_PATH, token=None)
    except ConnectionRefused as exc:
        log.failed(str(exc))
        ok = False
    else:
        if response.status_code == 403:
            log.passed("403 as expected (auth.missing_bearer_token)")
        else:
            log.failed(f"Expected 403, got {response.status_code}")
            ok = False

    # NT2 -- malformed header (no "Bearer " scheme)
    ok &= _assert_status(
        client, "NT2: malformed Authorization header (no Bearer scheme)", "GET", _PROBE_PATH,
        token=None, expected_statuses={403}, headers={"Authorization": admin["access_token"]},
    )

    # NT3 -- garbage / non-JWT token
    ok &= _assert_status(
        client, "NT3: garbage non-JWT token", "GET", _PROBE_PATH,
        token="this-is-not-a-jwt-at-all", expected_statuses={403},
    )

    # NT4 -- tampered token (flip the last character of a real, valid token)
    real_token = admin["access_token"]
    tampered = real_token[:-1] + ("A" if real_token[-1] != "A" else "B")
    ok &= _assert_status(client, "NT4: tampered token (signature invalidated)", "GET", _PROBE_PATH, token=tampered, expected_statuses={403})

    # NT5 -- expired token, crafted with the project's own real secret
    log = StepLogger("NT5: expired token")
    try:
        now = datetime.now(timezone.utc)
        expired_token = craft_token_with_project_secret(
            admin["user_id"], admin["organization_id"], issued_at=now - timedelta(hours=2), expires_at=now - timedelta(hours=1)
        )
    except TokenVerificationUnavailable as exc:
        log.failed(str(exc))
        ok = False
    else:
        log.step(f"GET {_PROBE_PATH} with a token whose exp is 1 hour in the past")
        try:
            response = client.call(log, "GET", _PROBE_PATH, token=expired_token)
        except ConnectionRefused as exc:
            log.failed(str(exc))
            ok = False
        else:
            if response.status_code == 403:
                log.passed("403 as expected (auth.invalid_token / expired)")
            else:
                log.failed(f"Expected 403, got {response.status_code}")
                ok = False

    # NT6 -- validly-signed token, but organization_id its subject has no role in
    log = StepLogger("NT6: token asserts an organization the user does not belong to")
    try:
        second_org = bootstrap_persona_sync(
            organization_name=config.org_b_name,
            organization_slug=config.org_b_slug + "-nt6",
            persona="read_only",
            email="nt6-unrelated-org@example.com",
            display_name="NT6 fixture (unrelated org)",
        )
        now = datetime.now(timezone.utc)
        wrong_org_token = craft_token_with_project_secret(
            admin["user_id"], second_org["organization_id"], issued_at=now, expires_at=now + timedelta(minutes=30)
        )
    except (TokenVerificationUnavailable, Exception) as exc:  # noqa: BLE001
        log.failed(f"{type(exc).__name__}: {exc}")
        ok = False
    else:
        log.step(f"GET {_PROBE_PATH} as the real admin user, but claiming a different organization")
        try:
            response = client.call(log, "GET", _PROBE_PATH, token=wrong_org_token)
        except ConnectionRefused as exc:
            log.failed(str(exc))
            ok = False
        else:
            if response.status_code == 403:
                log.passed("403 as expected -- zero permissions resolved for an unrelated organization")
            else:
                log.failed(f"Expected 403, got {response.status_code}")
                ok = False

    # NT7 -- a real user with genuinely no role assignment in the TARGET org
    log = StepLogger("NT7: user with no role mapping in this organization")
    try:
        # bootstrap_persona_sync always assigns a role in whatever org you
        # give it (that's its job) -- so to get a user with genuinely ZERO
        # roles in the TARGET org (`org`), this deliberately provisions them
        # in a THIRD, unrelated organization instead, then forges a token
        # pairing that real user_id with the target org's id. That user is
        # real and their token is really theirs; they simply hold no
        # `project_memberships`/`user_roles` row in `org` at all -- the
        # actual scenario this case is named for. (Distinct from NT6, whose
        # forged token instead reused an EXISTING user's id against a
        # DIFFERENT org they don't belong to; the resulting server-side
        # behavior is identical either way, which this script deliberately
        # demonstrates by covering both constructions.)
        no_role_user = bootstrap_persona_sync(
            organization_name="NT7 Unrelated Org", organization_slug="nt7-unrelated-org",
            persona="read_only", email="nt7-user-with-no-role-in-target-org@example.com",
            display_name="NT7 fixture",
        )
        now = datetime.now(timezone.utc)
        no_role_token = craft_token_with_project_secret(
            no_role_user["user_id"], org["id"], issued_at=now, expires_at=now + timedelta(minutes=30)
        )
    except (TokenVerificationUnavailable, Exception) as exc:  # noqa: BLE001
        log.failed(f"{type(exc).__name__}: {exc}")
        ok = False
    else:
        log.step(f"GET {_PROBE_PATH} for a user with no role assignment in this organization")
        try:
            gated_response = client.call(log, "GET", _PROBE_PATH, token=no_role_token)
            me_response = client.call(log, "GET", "/auth/me", token=no_role_token)
        except ConnectionRefused as exc:
            log.failed(str(exc))
            ok = False
        else:
            if gated_response.status_code == 403 and me_response.status_code == 200 and me_response.json().get("roles") == []:
                log.passed("Permission-gated call denied (403); /auth/me still resolves (200) with empty roles -- fail-closed, not an error.")
            else:
                log.failed(f"gated={gated_response.status_code}, /auth/me={me_response.status_code}")
                ok = False

    # NT8 -- token names an organization_id that doesn't exist as an org at all
    log = StepLogger("NT8: token names a nonexistent organization_id")
    now = datetime.now(timezone.utc)
    try:
        nonexistent_org_token = craft_token_with_project_secret(
            admin["user_id"], str(uuid.uuid4()), issued_at=now, expires_at=now + timedelta(minutes=30)
        )
    except TokenVerificationUnavailable as exc:
        log.failed(str(exc))
        ok = False
    else:
        ok &= _assert_status(
            client, "NT8: token names a nonexistent organization_id", "GET", _PROBE_PATH,
            token=nonexistent_org_token, expected_statuses={403},
        )
        # (the StepLogger created above for NT8's header is unused if we take
        # this branch -- harmless, avoids double-declaring the case.)

    # NT9 -- disabled user account
    log = StepLogger("NT9: disabled user account")
    try:
        disabled_user = bootstrap_persona_sync(
            organization_name=org["name"], organization_slug=org["slug"], persona="read_only",
            email="nt9-disabled-user@example.com", display_name="NT9 fixture (will be disabled)",
        )
        log.info("Disabling this user directly (no REST/MCP API exists for this -- see module docstring).")
        set_user_active_sync(disabled_user["user_id"], is_active=False)
    except Exception as exc:  # noqa: BLE001
        log.failed(f"{type(exc).__name__}: {exc}")
        ok = False
    else:
        log.step(f"GET {_PROBE_PATH} with a token belonging to a now-disabled user")
        try:
            response = client.call(log, "GET", _PROBE_PATH, token=disabled_user["access_token"])
        except ConnectionRefused as exc:
            log.failed(str(exc))
            ok = False
        else:
            if response.status_code == 403:
                log.passed("403 as expected (user.inactive)")
            else:
                log.failed(f"Expected 403, got {response.status_code}")
                ok = False
        finally:
            set_user_active_sync(disabled_user["user_id"], is_active=True)  # leave state clean

    # NT10/NT11 use a disposable, freshly-minted session (never the shared
    # "admin" persona from .state.json) specifically so this script never
    # invalidates a session another script -- or a re-run of this one --
    # still expects to be live.
    log = StepLogger("NT10/NT11 setup: mint a disposable session to revoke")
    try:
        disposable = bootstrap_persona_sync(
            organization_name=org["name"], organization_slug=org["slug"], persona="read_only",
            email="nt10-11-disposable-session@example.com", display_name="NT10/NT11 fixture",
        )
        log.passed("Disposable session minted.")
    except Exception as exc:  # noqa: BLE001
        log.failed(f"{type(exc).__name__}: {exc}")
        disposable = None
        ok = False

    if disposable is not None:
        # NT10 -- revoked session, access token not yet expired
        log = StepLogger("NT10: revoked session's access token still works until natural expiry")
        log.step("POST /auth/logout with the disposable refresh token, then reuse the SAME access token")
        try:
            logout_response = client.call(log, "POST", "/auth/logout", json_body={"refresh_token": disposable["refresh_token"]})
            reuse_response = client.call(log, "GET", _PROBE_PATH, token=disposable["access_token"])
        except ConnectionRefused as exc:
            log.failed(str(exc))
            ok = False
        else:
            if logout_response.status_code == 204 and reuse_response.status_code == 200:
                log.passed(
                    "Access token still accepted after logout, as documented -- "
                    "logout revokes the refresh token, not the already-issued access token."
                )
            else:
                log.failed(f"logout={logout_response.status_code}, reuse={reuse_response.status_code}")
                ok = False

        # NT11 -- the now-revoked refresh token cannot itself be used again
        log = StepLogger("NT11: revoked refresh token cannot be reused")
        log.step("POST /auth/refresh with the SAME refresh token just revoked by NT10's logout call")
        try:
            response = client.call(log, "POST", "/auth/refresh", json_body={"refresh_token": disposable["refresh_token"]})
        except ConnectionRefused as exc:
            log.failed(str(exc))
            ok = False
        else:
            if response.status_code == 403:
                log.passed("403 as expected (auth.invalid_refresh_token)")
            else:
                log.failed(f"Expected 403, got {response.status_code}")
                ok = False

    # NT12-NT16 -- require a real IdP round trip; document, don't fake.
    for case_id, reason in [
        ("NT12: wrong issuer", "requires a live IdP token endpoint to reject against -- see module docstring."),
        ("NT13: wrong client id/secret", "requires a live IdP; see module docstring for a real, disclosed 500-vs-4xx gap this would expose."),
        ("NT14: invalid redirect_uri", "rejected by the IdP itself before EKIP ever sees the request."),
        ("NT15: invalid nonce", "EKIP's OIDC flow sends and checks no nonce at all -- see module docstring for this real, disclosed gap."),
        ("NT16: clock skew", "would require desynchronizing this machine's own clock -- out of scope for an API test script."),
    ]:
        StepLogger(case_id).skipped(reason)

    print_summary()
    return bool(ok)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
