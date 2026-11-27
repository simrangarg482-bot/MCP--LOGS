"""A suspended organization over real HTTP: the real FastAPI app, real PostgreSQL as `ekip_app`, real
tokens. Status is flipped with a direct superuser UPDATE (the lifecycle APIs arrive in Phase 5).

Skipped unless EKIP_TEST_* and DATABASE_URL point at a LOCAL migrated database."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

import app.api.rate_limit as rate_limit_module
from app.api import main as api_main
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
    monkeypatch.setattr(rate_limit_module, "_limiter", TokenBucketRateLimiter())
    with TestClient(api_main.app, raise_server_exceptions=False) as client:
        yield client
    from app.database import session as db_session

    db_session.engine.sync_engine.dispose(close=False)


def _bearer(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest.mark.asyncio
async def test_suspension_over_http_is_immediate_scoped_and_complete(pw, api) -> None:  # noqa: F811
    a, b = await _org(pw, "ha"), await _org(pw, "hb")
    admin = await _user(pw, "admin", a, b)
    await pw.sup(
        "INSERT INTO user_roles (user_id, organization_id, role_id) SELECT :u, :o, id FROM roles WHERE name='admin' AND organization_id IS NULL",
        u=admin, o=a,
    )
    sole = await _user(pw, "sole", a)
    email, sole_email = pw.email("admin"), pw.email("sole")

    # sign in to both organizations through the real picker
    picker = api.post("/auth/login", json={"email": email, "password": OLD_PW}).json()
    assert picker["status"] == "organization_selection_required"
    pick = lambda org: api.post(  # noqa: E731
        "/auth/select-organization", json={"selection_token": picker["selection_token"], "organization_id": str(org)}
    ).json()
    in_a, in_b = pick(a), pick(b)
    sole_a = api.post("/auth/login", json={"email": sole_email, "password": OLD_PW}).json()
    assert api.get("/auth/me", headers=_bearer(in_a)).status_code == 200  # onboarding organizations work

    await pw.sup("UPDATE organizations SET status='suspended' WHERE id=:o", o=a)

    # an access token issued BEFORE the suspension is refused on the very next request
    for tokens in (in_a, sole_a):
        r = api.get("/auth/me", headers=_bearer(tokens))
        assert r.status_code == 403 and r.json()["error_code"] == "auth.organization_suspended"
    assert api.get(f"/organizations/{a}/projects", headers=_bearer(in_a)).status_code == 403
    # ... even an administrator cannot act: invitations, settings, everything is behind the same check
    invite = api.post(f"/organizations/{a}/invitations", headers=_bearer(in_a), json={"email": pw.email("x"), "grants_role": "viewer"})
    assert invite.status_code == 403 and invite.json()["error_code"] == "auth.organization_suspended"

    # the same person's token for the OTHER organization is unaffected
    assert api.get("/auth/me", headers=_bearer(in_b)).status_code == 200

    # no way back in: refresh, password login, selection, switching
    r = api.post("/auth/refresh", json={"refresh_token": in_a["refresh_token"]})
    assert r.status_code == 403 and r.json()["error_code"] == "auth.organization_suspended"
    assert api.post("/auth/refresh", json={"refresh_token": in_b["refresh_token"]}).status_code == 200
    sole_login = api.post("/auth/login", json={"email": sole_email, "password": OLD_PW})
    assert sole_login.status_code == 403 and sole_login.json()["error_code"] == "auth.organization_suspended"
    direct = api.post("/auth/login", json={"email": email, "password": OLD_PW})
    assert direct.status_code == 200 and direct.json()["status"] == "complete"  # straight into the active one, no picker
    assert api.get("/auth/me", headers=_bearer(direct.json())).status_code == 200
    switch = api.post("/auth/switch-organization", headers=_bearer(in_b), json={"organization_id": str(a)})
    assert switch.status_code == 403 and switch.json()["error_code"] == "auth.organization_suspended"
    orgs = api.get("/auth/organizations", headers=_bearer(in_b)).json()["organizations"]
    assert [o["id"] for o in orgs] == [str(b)]

    # logging out of a suspended session is still allowed (it needs no identity)
    assert api.post("/auth/logout", json={"refresh_token": in_a["refresh_token"]}).status_code == 204
    assert sole and admin  # (accounts exist; only their organization is suspended)
