"""10 -- Logout, "logout everywhere", refresh rotation, and re-login.

PURPOSE
    Exercise the full session lifecycle beyond the single revoked-token
    check already covered by 09_negative_tests.py's NT10/NT11: refresh
    rotation (a valid refresh token exchanges cleanly for a new pair),
    self-service "logout everywhere" (a user revoking every one of their
    OWN sessions), admin-triggered "logout everywhere" (an org admin
    forcing a DIFFERENT user's sessions to end), and that a fresh,
    unrelated login still works afterward (revocation is scoped to the
    specific user, not global).

WHICH APIs IT CALLS (all real, live REST calls)
    POST {BASE_URL}/auth/refresh
    POST {BASE_URL}/auth/logout-all
    POST {BASE_URL}/users/{user_id}/logout-all

WHAT IT CHECKS
    LT1  A valid refresh token rotates cleanly: POST /auth/refresh returns
         a NEW access+refresh token pair, and the OLD refresh token is now
         itself invalid (single-use rotation).
    LT2  Self-service logout-all: a user's own POST /auth/logout-all
         revokes their session; their (now-superseded) refresh token no
         longer works via POST /auth/refresh.
    LT3  Admin-triggered logout-all: an org admin can force a DIFFERENT
         user's sessions to end via POST /users/{user_id}/logout-all; that
         user's refresh token then fails the same way.
    LT4  A non-admin user CANNOT call POST /users/{user_id}/logout-all for
         someone else -- expect 403 (requires tenancy:manage, per
         `app/api/routers/users.py`).
    LT5  A fresh login for the SAME user after being logged out still
         works (revocation is per-session-family, not a permanent lockout)
         -- modeled here via `common.bootstrap` minting a brand-new session
         for that user, the same non-API mechanism used throughout this
         harness in place of a real interactive SSO login.

EXPECTED INPUT
    .state.json's "org" and "users.admin" (Scripts 01-02).

HOW TO EXECUTE
    python scripts/realworld_onboarding/10_logout_tests.py

EXPECTED SUCCESS OUTPUT
    "5/5 checks passed."
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.bootstrap import bootstrap_persona_sync  # noqa: E402
from common.config import load_config  # noqa: E402
from common.http import ApiClient, ConnectionRefused  # noqa: E402
from common.logger import StepLogger, print_summary  # noqa: E402
from common.state import load_state  # noqa: E402


def main() -> bool:
    config = load_config()
    state = load_state()
    org = state.get("org")
    admin = state.get("users", {}).get("admin")
    if not org or not admin:
        StepLogger("Logout Tests").failed("Missing .state.json 'org'/'users.admin' -- run 01/02 first.")
        print_summary()
        return False

    client = ApiClient(config.base_url, config.request_timeout_seconds)
    ok = True

    # --- LT1: refresh rotation ------------------------------------------------
    log = StepLogger("LT1: refresh token rotation")
    try:
        fixture_lt1 = bootstrap_persona_sync(
            organization_name=org["name"], organization_slug=org["slug"], persona="developer",
            email="lt1-refresh-rotation@example.com", display_name="LT1 fixture",
        )
    except Exception as exc:  # noqa: BLE001
        log.failed(f"{type(exc).__name__}: {exc}")
        ok = False
    else:
        log.step("POST /auth/refresh with a valid refresh token")
        try:
            refresh_response = client.call(log, "POST", "/auth/refresh", json_body={"refresh_token": fixture_lt1["refresh_token"]})
        except ConnectionRefused as exc:
            log.failed(str(exc))
            ok = False
        else:
            if refresh_response.status_code != 200:
                log.failed(f"Expected 200, got {refresh_response.status_code}")
                ok = False
            else:
                new_tokens = refresh_response.json()
                log.step("Re-using the OLD refresh token (already rotated away) -- expect denial")
                try:
                    reuse_response = client.call(log, "POST", "/auth/refresh", json_body={"refresh_token": fixture_lt1["refresh_token"]})
                except ConnectionRefused as exc:
                    log.failed(str(exc))
                    ok = False
                else:
                    if new_tokens["access_token"] != fixture_lt1["access_token"] and reuse_response.status_code == 403:
                        log.passed("New token pair issued; old refresh token now correctly rejected.")
                    else:
                        log.failed(f"reuse={reuse_response.status_code}")
                        ok = False

    # --- LT2: self-service logout-all -----------------------------------------
    log = StepLogger("LT2: self-service POST /auth/logout-all")
    try:
        fixture_lt2 = bootstrap_persona_sync(
            organization_name=org["name"], organization_slug=org["slug"], persona="developer",
            email="lt2-self-logout-all@example.com", display_name="LT2 fixture",
        )
    except Exception as exc:  # noqa: BLE001
        log.failed(f"{type(exc).__name__}: {exc}")
        ok = False
    else:
        log.step("POST /auth/logout-all as the user themselves")
        try:
            logout_all_response = client.call(log, "POST", "/auth/logout-all", token=fixture_lt2["access_token"])
            refresh_after = client.call(log, "POST", "/auth/refresh", json_body={"refresh_token": fixture_lt2["refresh_token"]})
        except ConnectionRefused as exc:
            log.failed(str(exc))
            ok = False
        else:
            if logout_all_response.status_code == 200 and refresh_after.status_code == 403:
                log.passed(f"logout-all revoked {logout_all_response.json().get('revoked_session_count')} session(s); refresh now denied.")
            else:
                log.failed(f"logout-all={logout_all_response.status_code}, refresh_after={refresh_after.status_code}")
                ok = False

    # --- LT3: admin-triggered logout-all for a DIFFERENT user -----------------
    log = StepLogger("LT3: admin POST /users/{user_id}/logout-all for another user")
    try:
        fixture_lt3 = bootstrap_persona_sync(
            organization_name=org["name"], organization_slug=org["slug"], persona="developer",
            email="lt3-admin-forced-logout@example.com", display_name="LT3 fixture",
        )
    except Exception as exc:  # noqa: BLE001
        log.failed(f"{type(exc).__name__}: {exc}")
        ok = False
    else:
        log.step(f"POST /users/{fixture_lt3['user_id']}/logout-all as the org admin")
        try:
            forced_response = client.call(log, "POST", f"/users/{fixture_lt3['user_id']}/logout-all", token=admin["access_token"])
            refresh_after = client.call(log, "POST", "/auth/refresh", json_body={"refresh_token": fixture_lt3["refresh_token"]})
        except ConnectionRefused as exc:
            log.failed(str(exc))
            ok = False
        else:
            if forced_response.status_code == 200 and refresh_after.status_code == 403:
                log.passed("Admin successfully forced this user's session(s) to end; refresh now denied.")
            else:
                log.failed(f"forced={forced_response.status_code}, refresh_after={refresh_after.status_code}")
                ok = False

        # --- LT4: a NON-admin cannot do the same -------------------------------
        log = StepLogger("LT4: non-admin cannot force another user's logout-all")
        try:
            non_admin = bootstrap_persona_sync(
                organization_name=org["name"], organization_slug=org["slug"], persona="read_only",
                email="lt4-non-admin@example.com", display_name="LT4 fixture (no tenancy:manage)",
            )
        except Exception as exc:  # noqa: BLE001
            log.failed(f"{type(exc).__name__}: {exc}")
            ok = False
        else:
            log.step(f"POST /users/{fixture_lt3['user_id']}/logout-all as a read_only (non-admin) user")
            try:
                denied_response = client.call(log, "POST", f"/users/{fixture_lt3['user_id']}/logout-all", token=non_admin["access_token"])
            except ConnectionRefused as exc:
                log.failed(str(exc))
                ok = False
            else:
                if denied_response.status_code == 403:
                    log.passed("403 as expected -- lacks tenancy:manage.")
                else:
                    log.failed(f"Expected 403, got {denied_response.status_code}")
                    ok = False

    # --- LT5: a fresh login for the same user still works afterward ----------
    log = StepLogger("LT5: fresh login still works after being logged out")
    log.step("Minting a brand-new session for the LT3 user (stands in for a real re-login -- see README).")
    try:
        relogin = bootstrap_persona_sync(
            organization_name=org["name"], organization_slug=org["slug"], persona="developer",
            email="lt3-admin-forced-logout@example.com", display_name="LT3 fixture",
        )
        me_response = client.call(log, "GET", "/auth/me", token=relogin["access_token"])
    except ConnectionRefused as exc:
        log.failed(str(exc))
        ok = False
    else:
        if me_response.status_code == 200:
            log.passed("Fresh session works normally -- being logged out is not a permanent lockout.")
        else:
            log.failed(f"Expected 200, got {me_response.status_code}")
            ok = False

    print_summary()
    return bool(ok)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
