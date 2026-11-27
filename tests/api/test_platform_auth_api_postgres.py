"""Platform operator authentication over real HTTP: the real FastAPI app, real PostgreSQL as `ekip_app`.

Skipped unless EKIP_TEST_* and DATABASE_URL point at a LOCAL migrated database."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from jose import jwt as jose_jwt
from sqlalchemy import text

import app.api.rate_limit as rate_limit_module
from app.api import main as api_main
from app.core.auth import service as auth_service
from app.core.platform import auth as platform_auth
from app.shared.config.settings import get_settings
from app.shared.rate_limiter import TokenBucketRateLimiter
from tests.core.auth.test_password_reset_postgres import (  # noqa: F401  (fixtures + helpers)
    OLD_PW,
    _APP_URL,
    _SUPER_URL,
    _is_local,
    _org,
    _user,
    env,
    pw,
)
from tests.core.users.test_role_management_postgres import _engine

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


def _enable(monkeypatch, enabled=True):
    settings = get_settings().model_copy(update={"platform_admin_enabled": enabled})
    monkeypatch.setattr(platform_auth, "get_settings", lambda: settings)
    return settings


def _dispose_engine():
    from app.database import session as db_session

    db_session.engine.sync_engine.dispose(close=False)


@pytest.fixture()
def api(monkeypatch):
    monkeypatch.setattr(rate_limit_module, "_limiter", TokenBucketRateLimiter())
    _enable(monkeypatch, True)
    with TestClient(api_main.app, raise_server_exceptions=False) as client:
        yield client
    _dispose_engine()


@pytest_asyncio.fixture(autouse=True)
async def _purge(pw):  # noqa: F811
    migrated = await pw.sup("SELECT 1 FROM pg_proc WHERE proname='platform_operator_role'")
    if not migrated:
        pytest.skip("run `alembic upgrade head` on this database")
    yield
    like = f"rm-{pw.tag}-%"
    engine = _engine(_SUPER_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL session_replication_role = replica"))
            await conn.execute(
                text("DELETE FROM platform_audit_events WHERE actor_user_id IN (SELECT id FROM users WHERE email LIKE :e)"),
                {"e": like},
            )
    finally:
        await engine.dispose()


async def _operator(pw, name, role="admin", **user_kwargs):  # noqa: F811
    uid = await _user(pw, name, **user_kwargs)
    await pw.sup("INSERT INTO platform_operators (user_id, role) VALUES (:u, :r)", u=uid, r=role)
    return uid


def _login(api, pw, name, password=OLD_PW):  # noqa: F811
    return api.post("/platform/auth/login", json={"email": pw.email(name), "password": password})


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- kill switch


@pytest.mark.asyncio
async def test_disabled_by_default_everything_is_unavailable(pw, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(rate_limit_module, "_limiter", TokenBucketRateLimiter())
    assert get_settings().platform_admin_enabled is False  # the shipped default
    await _operator(pw, "off")
    with TestClient(api_main.app, raise_server_exceptions=False) as api:
        assert api.get("/platform/status").json() == {"available": False}
        r = api.post("/platform/auth/login", json={"email": pw.email("off"), "password": OLD_PW})
        assert r.status_code == 404 and r.json()["error_code"] == "platform.unavailable"
        assert api.get("/platform/auth/me", headers=_bearer("x")).json()["error_code"] == "platform.unavailable"
    _dispose_engine()


@pytest.mark.asyncio
async def test_the_kill_switch_cuts_off_an_already_issued_platform_token(pw, api, monkeypatch) -> None:  # noqa: F811
    await _operator(pw, "kill")
    token = _login(api, pw, "kill").json()["access_token"]
    assert api.get("/platform/auth/me", headers=_bearer(token)).status_code == 200
    _enable(monkeypatch, False)
    r = api.get("/platform/auth/me", headers=_bearer(token))
    assert r.status_code == 404 and r.json()["error_code"] == "platform.unavailable"
    assert api.get("/platform/status").json() == {"available": False}


# ---------------------------------------------------------------- login


@pytest.mark.asyncio
async def test_an_operator_signs_in_gets_a_short_token_and_no_refresh_token(pw, api) -> None:  # noqa: F811
    uid = await _operator(pw, "ok")
    r = _login(api, pw, "ok")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"access_token", "token_type", "expires_in", "role"}  # no refresh token, no organization
    assert body["expires_in"] == 15 * 60 and body["role"] == "admin"
    me = api.get("/platform/auth/me", headers=_bearer(body["access_token"]))
    assert me.status_code == 200 and me.json()["user_id"] == str(uid) and me.json()["role"] == "admin"
    assert "password" not in me.text and "hash" not in me.text
    assert (await pw.sup("SELECT count(*) FROM platform_audit_events WHERE actor_user_id=:u AND action='platform.login'", u=uid))[0][0] == 1
    assert (await pw.sup("SELECT count(*) FROM refresh_tokens WHERE user_id=:u", u=uid))[0][0] == 0


@pytest.mark.asyncio
async def test_every_login_failure_is_the_same_answer(pw, api) -> None:  # noqa: F811
    from app.api.routers import platform as platform_router

    api_main.app.dependency_overrides[platform_router._LOGIN_IP_LIMIT] = lambda: None  # 7 attempts from one address
    try:
        await _every_login_failure(pw, api)
    finally:
        api_main.app.dependency_overrides.pop(platform_router._LOGIN_IP_LIMIT, None)


async def _every_login_failure(pw, api) -> None:  # noqa: F811
    await _operator(pw, "real")
    await _user(pw, "plain")  # a normal account that is not an operator
    admin_org = await _org(pw, "tmorg")
    tm = await _user(pw, "tmadmin", admin_org)
    await pw.sup(
        "INSERT INTO user_roles (user_id, organization_id, role_id) SELECT :u, :o, id FROM roles WHERE name='admin' AND organization_id IS NULL",
        u=tm, o=admin_org,
    )
    revoked = await _operator(pw, "revoked")
    await pw.sup("UPDATE platform_operators SET revoked_at=now() WHERE user_id=:u", u=revoked)
    await _operator(pw, "inactive", active=False)
    await _operator(pw, "unverified", verified=False)
    cases = {
        "wrong password": _login(api, pw, "real", "not the password at all"),
        "unknown email": api.post("/platform/auth/login", json={"email": "nobody@example.invalid", "password": OLD_PW}),
        "not an operator": _login(api, pw, "plain"),
        "tenant admin (tenancy:manage)": _login(api, pw, "tmadmin"),
        "revoked operator": _login(api, pw, "revoked"),
        "inactive": _login(api, pw, "inactive"),
        "unverified": _login(api, pw, "unverified"),
    }
    bodies = {name: (r.status_code, r.json()["error_code"], r.json()["message"]) for name, r in cases.items()}
    assert set(bodies.values()) == {(403, "platform.invalid_credentials", "Invalid email or password.")}, bodies
    assert (await pw.sup("SELECT count(*) FROM platform_audit_events WHERE action='platform.login' AND actor_user_id IN (SELECT id FROM users WHERE email LIKE :e)", e=f"rm-{pw.tag}-%"))[0][0] == 0


@pytest.mark.asyncio
async def test_tenancy_manage_grants_nothing_on_the_platform(pw, api) -> None:  # noqa: F811
    org = await _org(pw, "tm2")
    tm = await _user(pw, "tm2admin", org)
    await pw.sup(
        "INSERT INTO user_roles (user_id, organization_id, role_id) SELECT :u, :o, id FROM roles WHERE name='admin' AND organization_id IS NULL",
        u=tm, o=org,
    )
    tenant = api.post("/auth/login", json={"email": pw.email("tm2admin"), "password": OLD_PW}).json()
    assert api.get("/auth/me", headers=_bearer(tenant["access_token"])).status_code == 200
    assert api.get("/platform/auth/me", headers=_bearer(tenant["access_token"])).status_code == 403
    assert _login(api, pw, "tm2admin").status_code == 403


# ---------------------------------------------------------------- token separation


@pytest.mark.asyncio
async def test_tenant_and_platform_tokens_do_not_cross(pw, api) -> None:  # noqa: F811
    org = await _org(pw, "sep")
    uid = await _user(pw, "sep", org)  # a tenant member who is ALSO a platform operator
    await pw.sup("INSERT INTO platform_operators (user_id, role) VALUES (:u, 'admin')", u=uid)
    platform_token = _login(api, pw, "sep").json()["access_token"]
    tenant = api.post("/auth/login", json={"email": pw.email("sep"), "password": OLD_PW}).json()

    r = api.get("/platform/auth/me", headers=_bearer(tenant["access_token"]))
    assert r.status_code == 403 and r.json()["error_code"] == "platform.invalid_token"
    for path in ("/auth/me", "/auth/organizations", f"/organizations/{org}/projects", "/incidents"):
        r = api.get(path, headers=_bearer(platform_token))
        assert r.status_code in (401, 403), path
        assert r.json()["error_code"] == "auth.invalid_token", path
    resource = "https://mcp.example/mcp"
    mcp_token, _i, _e = auth_service._issue_access_token(uid, org, scope="mcp", audience=resource, issuer=resource)
    assert api.get("/platform/auth/me", headers=_bearer(mcp_token)).status_code == 403
    assert api.get("/platform/auth/me").status_code == 403
    assert api.get("/platform/auth/me", headers={"Authorization": "Token abc"}).status_code == 403


# ---------------------------------------------------------------- per-request re-verification


@pytest.mark.asyncio
async def test_revocation_takes_effect_on_the_very_next_request(pw, api) -> None:  # noqa: F811
    uid = await _operator(pw, "rev")
    token = _login(api, pw, "rev").json()["access_token"]
    assert api.get("/platform/auth/me", headers=_bearer(token)).status_code == 200
    await pw.sup("UPDATE platform_operators SET revoked_at=now() WHERE user_id=:u", u=uid)
    r = api.get("/platform/auth/me", headers=_bearer(token))
    assert r.status_code == 403 and r.json()["error_code"] == "platform.access_denied"
    assert _login(api, pw, "rev").status_code == 403  # and cannot sign in again


@pytest.mark.asyncio
async def test_deactivated_account_and_password_change_end_the_session(pw, api) -> None:  # noqa: F811
    uid = await _operator(pw, "acct")
    token = _login(api, pw, "acct").json()["access_token"]
    await pw.sup("UPDATE users SET sessions_valid_after = now() + interval '1 second' WHERE id=:u", u=uid)
    r = api.get("/platform/auth/me", headers=_bearer(token))
    assert r.status_code == 403 and r.json()["error_code"] == "platform.session_revoked"
    await pw.sup("UPDATE users SET sessions_valid_after = NULL, is_active=false WHERE id=:u", u=uid)
    assert api.get("/platform/auth/me", headers=_bearer(token)).json()["error_code"] == "platform.access_denied"


@pytest.mark.asyncio
async def test_a_viewer_operator_is_reported_as_a_viewer(pw, api) -> None:  # noqa: F811
    await _operator(pw, "view", role="viewer")
    body = _login(api, pw, "view").json()
    assert body["role"] == "viewer"
    me = api.get("/platform/auth/me", headers=_bearer(body["access_token"])).json()
    assert me["role"] == "viewer"


@pytest.mark.asyncio
async def test_an_expired_platform_token_is_refused(pw, api) -> None:  # noqa: F811
    uid = await _operator(pw, "exp")
    s = get_settings()
    now = datetime.now(UTC)
    token = jose_jwt.encode(
        {"sub": str(uid), "type": "platform", "aud": "ekip-platform", "iss": "ekip-platform",
         "iat": int((now - timedelta(minutes=20)).timestamp()), "exp": int((now - timedelta(minutes=5)).timestamp())},
        s.jwt_secret_key, algorithm=s.jwt_algorithm,
    )
    r = api.get("/platform/auth/me", headers=_bearer(token))
    assert r.status_code == 403 and r.json()["error_code"] == "platform.invalid_token"


# ---------------------------------------------------------------- rate limiting


@pytest.mark.asyncio
async def test_login_is_rate_limited_by_address(pw, api) -> None:  # noqa: F811
    await _operator(pw, "rl")
    codes = [_login(api, pw, "rl", f"wrong-{i}").status_code for i in range(5)]
    assert codes == [403] * 5
    r = _login(api, pw, "rl", "wrong-again")
    assert r.status_code == 429 and r.json()["error_code"] == "rate_limited.ip"
    assert _login(api, pw, "rl").status_code == 429  # even the right password: guessing cannot be continued


@pytest.mark.asyncio
async def test_the_account_budget_holds_when_the_address_changes(pw, monkeypatch) -> None:  # noqa: F811
    from app.api.routers import platform as platform_router

    monkeypatch.setattr(rate_limit_module, "_limiter", TokenBucketRateLimiter())
    _enable(monkeypatch, True)
    await _operator(pw, "acc")
    with TestClient(api_main.app, raise_server_exceptions=False) as api:
        # lift only the per-address limit; the per-account limit must still stop a distributed guesser
        api_main.app.dependency_overrides[platform_router._LOGIN_IP_LIMIT] = lambda: None
        try:
            codes = [_login(api, pw, "acc", f"wrong-{i}").status_code for i in range(6)]
        finally:
            api_main.app.dependency_overrides.pop(platform_router._LOGIN_IP_LIMIT, None)
    assert codes[:5] == [403] * 5 and codes[5] == 429
    _dispose_engine()


@pytest.mark.asyncio
async def test_authenticated_platform_requests_are_rate_limited_per_operator(pw, api) -> None:  # noqa: F811
    await _operator(pw, "rlo")
    token = _login(api, pw, "rlo").json()["access_token"]
    statuses = [api.get("/platform/auth/me", headers=_bearer(token)).status_code for _ in range(125)]
    assert statuses[0] == 200 and 429 in statuses
