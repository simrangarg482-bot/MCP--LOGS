"""The password flows end to end through the real FastAPI app against real PostgreSQL (`ekip_app`):
real request lifecycle (dependency commit, then BackgroundTasks), real tokens, real `/auth/me`.
Only the SMTP sender is replaced by a recorder -- no email is ever sent.

Skipped unless EKIP_TEST_* and DATABASE_URL point at a LOCAL migrated database."""

from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

import app.api.rate_limit as rate_limit_module
from app.api import main as api_main
from app.core.auth import password_reset
from app.shared.rate_limiter import TokenBucketRateLimiter
from tests.core.auth.test_password_reset_postgres import (  # noqa: F401  (fixtures + helpers)
    NEW_PW,
    OLD_PW,
    FakeSender,
    _APP_URL,
    _SUPER_URL,
    _is_local,
    _org,
    _user,
    env,
    pw,
)

pytestmark = pytest.mark.skipif(
    not (
        _SUPER_URL
        and _APP_URL
        and _is_local(_SUPER_URL)
        and _is_local(_APP_URL)
        and os.environ.get("DATABASE_URL", "").split("@")[-1].split("/")[0].split(":")[0] in {"localhost", "127.0.0.1"}
    ),
    reason="needs a LOCAL migrated database for EKIP_TEST_* and DATABASE_URL",
)


@pytest.fixture()
def api(monkeypatch):
    monkeypatch.setattr("app.api.deps.email_delivery_configured", lambda: True)
    monkeypatch.setattr(rate_limit_module, "_limiter", TokenBucketRateLimiter())
    sender = FakeSender()
    monkeypatch.setattr(password_reset, "get_email_sender", lambda: sender)
    # one portal (event loop) for the whole test, then drop pooled connections of that loop
    with TestClient(api_main.app, raise_server_exceptions=False) as client:
        yield client, sender
    from app.database import session as db_session

    db_session.engine.sync_engine.dispose(close=False)


def _bearer(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest.mark.asyncio
async def test_forgot_reset_login_and_old_sessions_over_http(pw, api) -> None:  # noqa: F811
    client, sender = api
    org = await _org(pw, "http")
    uid = await _user(pw, "http", org)
    email = pw.email("http")

    # a session that exists BEFORE the reset
    old = client.post("/auth/login", json={"email": email, "password": OLD_PW}).json()
    assert client.get("/auth/me", headers=_bearer(old)).status_code == 200

    # forgot: identical 202 for a real and an unknown address; mail only for the real one
    real = client.post("/auth/password/forgot", json={"email": email})
    ghost = client.post("/auth/password/forgot", json={"email": pw.email("nobody-here")})
    assert real.status_code == ghost.status_code == 202 and real.json() == ghost.json()
    (mail,) = sender.sent  # the BackgroundTask ran after the response
    assert mail.to == email
    token = re.search(r"#token=([\w\-]+)", mail.text).group(1)

    # a weak password is refused and does not burn the link
    weak = client.post("/auth/password/reset", json={"token": token, "new_password": "short"})
    assert weak.status_code == 400 and weak.json()["error_code"] == "auth.password_too_short"

    done = client.post("/auth/password/reset", json={"token": token, "new_password": NEW_PW})
    assert done.status_code == 200 and "access_token" not in done.text
    assert len(sender.sent) == 2 and "http" not in sender.sent[1].text  # the "password changed" notice carries no link

    # the link is single use
    again = client.post("/auth/password/reset", json={"token": token, "new_password": "another good password"})
    assert again.status_code == 409 and again.json()["error_code"] == "auth.reset_token_used"

    # the old session is dead at once (access AND refresh); the old password no longer works
    revoked = client.get("/auth/me", headers=_bearer(old))
    assert revoked.status_code == 403 and revoked.json()["error_code"] == "auth.session_revoked"
    assert client.post("/auth/refresh", json={"refresh_token": old["refresh_token"]}).status_code == 403
    assert client.post("/auth/login", json={"email": email, "password": OLD_PW}).status_code == 403
    fresh = client.post("/auth/login", json={"email": email, "password": NEW_PW})
    assert fresh.status_code == 200
    assert client.get("/auth/me", headers=_bearer(fresh.json())).status_code == 200


@pytest.mark.asyncio
async def test_change_password_over_http_swaps_the_session(pw, api) -> None:  # noqa: F811
    client, sender = api
    org = await _org(pw, "chg")
    await _user(pw, "chg", org)
    email = pw.email("chg")
    first = client.post("/auth/login", json={"email": email, "password": OLD_PW}).json()
    second = client.post("/auth/login", json={"email": email, "password": OLD_PW}).json()  # "another device"

    wrong = client.post("/auth/password/change", headers=_bearer(first), json={"current_password": "not my password!", "new_password": NEW_PW})
    assert wrong.status_code == 403 and wrong.json()["error_code"] == "auth.current_password_incorrect"
    assert client.get("/auth/me", headers=_bearer(first)).status_code == 200  # a failed attempt ends nothing

    ok = client.post("/auth/password/change", headers=_bearer(first), json={"current_password": OLD_PW, "new_password": NEW_PW})
    assert ok.status_code == 200
    new = ok.json()
    assert client.get("/auth/me", headers=_bearer(new)).status_code == 200
    for stale in (first, second):
        assert client.get("/auth/me", headers=_bearer(stale)).status_code == 403
        assert client.post("/auth/refresh", json={"refresh_token": stale["refresh_token"]}).status_code == 403
    assert client.post("/auth/refresh", json={"refresh_token": new["refresh_token"]}).status_code == 200
    assert len(sender.sent) == 1 and sender.sent[0].subject == "Your EKIP password was changed"
    # no token, no auth header -> no change
    assert client.post("/auth/password/change", json={"current_password": OLD_PW, "new_password": NEW_PW}).status_code in (401, 403)


@pytest.mark.asyncio
async def test_suspended_organization_over_http_password_login_and_refresh(pw, api) -> None:  # noqa: F811
    client, _ = api
    live, dead = await _org(pw, "hl"), await _org(pw, "hd")
    await _user(pw, "susp", live, dead)
    email = pw.email("susp")
    picker = client.post("/auth/login", json={"email": email, "password": OLD_PW}).json()
    assert picker["status"] == "organization_selection_required"
    chosen = client.post("/auth/select-organization", json={"selection_token": picker["selection_token"], "organization_id": str(dead)}).json()
    await pw.sup("UPDATE organizations SET status='suspended' WHERE id=:o", o=dead)

    # straight into the active organization now; the suspended one is neither offered nor reachable
    direct = client.post("/auth/login", json={"email": email, "password": OLD_PW})
    assert direct.status_code == 200 and direct.json()["status"] == "complete"
    blocked = client.post("/auth/refresh", json={"refresh_token": chosen["refresh_token"]})
    assert blocked.status_code == 403 and blocked.json()["error_code"] == "auth.organization_suspended"
    await pw.sup("UPDATE organizations SET status='suspended' WHERE id=:o", o=live)
    nothing = client.post("/auth/login", json={"email": email, "password": OLD_PW})
    assert nothing.status_code == 403 and nothing.json()["error_code"] == "auth.organization_suspended"
