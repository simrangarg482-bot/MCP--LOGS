"""Member offboarding over real HTTP: the real FastAPI app and real PostgreSQL as `ekip_app`, real tokens.

Skipped unless EKIP_TEST_* and DATABASE_URL point at a LOCAL migrated database."""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
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


@pytest_asyncio.fixture(autouse=True)
async def _migrated(pw):  # noqa: F811
    if not await pw.sup("SELECT 1 FROM information_schema.tables WHERE table_name='organization_member_states'"):
        pytest.skip("run `alembic upgrade head` on this database")
    yield


async def _admin(pw, name, *orgs):  # noqa: F811
    uid = await _user(pw, name, *orgs)
    for org in orgs:
        await pw.sup(
            "INSERT INTO user_roles (user_id, organization_id, role_id) SELECT :u, :o, id FROM roles "
            "WHERE name='admin' AND organization_id IS NULL ON CONFLICT DO NOTHING",
            u=uid, o=org,
        )
    return uid


def _login(api, pw, name, org=None):  # noqa: F811
    r = api.post("/auth/login", json={"email": pw.email(name), "password": OLD_PW})
    body = r.json()
    if body.get("status") == "organization_selection_required":
        body = api.post(
            "/auth/select-organization", json={"selection_token": body["selection_token"], "organization_id": str(org)}
        ).json()
    return body


def _h(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _url(org, user, verb):
    return f"/organizations/{org}/members/{user}/{verb}"


@pytest.mark.asyncio
async def test_deactivate_and_reactivate_over_http_cut_access_immediately_and_for_good(pw, api) -> None:  # noqa: F811
    org, other = await _org(pw, "http"), await _org(pw, "httpother")
    admin = await _admin(pw, "httpadmin", org)
    member = await _user(pw, "httpmember", org, other)
    admin_t = _login(api, pw, "httpadmin")
    member_a, member_b = _login(api, pw, "httpmember", org), _login(api, pw, "httpmember", other)
    assert api.get("/auth/me", headers=_h(member_a)).status_code == 200

    status = api.get(_url(org, member, "status"), headers=_h(admin_t))
    assert status.status_code == 200 and status.json()["status"] == "active" and status.json()["member_epoch"] == 0

    r = api.post(_url(org, member, "deactivate"), headers=_h(admin_t), json={"reason": "left the company"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["status"], body["source"], body["reason"], body["member_epoch"]) == ("deactivated", "admin", "left the company", 1)
    assert body["user_id"] == str(member) and body["organization_id"] == str(org)

    # "200" already means locked out (the change is committed before the response): the very next request fails
    refused = api.get("/auth/me", headers=_h(member_a))
    assert refused.status_code == 403 and refused.json()["error_code"] == "auth.membership_deactivated"
    assert api.get(f"/organizations/{org}/projects", headers=_h(member_a)).status_code == 403
    assert api.post("/auth/refresh", json={"refresh_token": member_a["refresh_token"]}).json()["error_code"] == "auth.membership_deactivated"
    login = api.post("/auth/login", json={"email": pw.email("httpmember"), "password": OLD_PW})
    assert login.status_code == 200 and login.json()["status"] == "complete"  # straight into the organization that still works
    # the same person's OTHER organization is untouched
    assert api.get("/auth/me", headers=_h(member_b)).status_code == 200
    assert api.post("/auth/refresh", json={"refresh_token": member_b["refresh_token"]}).status_code == 200
    # the administrator still sees what was recorded
    assert api.get(_url(org, member, "status"), headers=_h(admin_t)).json()["reason"] == "left the company"

    r = api.post(_url(org, member, "reactivate"), headers=_h(admin_t), json={"reason": "rehired"})
    assert r.status_code == 200 and r.json()["status"] == "active" and r.json()["member_epoch"] == 2
    stale = api.get("/auth/me", headers=_h(member_a))
    assert stale.status_code == 403 and stale.json()["error_code"] == "auth.session_revoked"  # NOT revived
    assert api.post("/auth/refresh", json={"refresh_token": member_a["refresh_token"]}).status_code == 403
    fresh = api.post("/auth/login", json={"email": pw.email("httpmember"), "password": OLD_PW}).json()
    if fresh.get("status") == "organization_selection_required":
        fresh = api.post("/auth/select-organization", json={"selection_token": fresh["selection_token"], "organization_id": str(org)}).json()
    assert api.get("/auth/me", headers=_h(fresh)).status_code == 200  # signing in again works
    assert admin


@pytest.mark.asyncio
async def test_validation_and_state_errors(pw, api) -> None:  # noqa: F811
    org = await _org(pw, "val")
    admin = await _admin(pw, "valadmin", org)
    other_admin = await _admin(pw, "valadmin2", org)
    member = await _user(pw, "valmember", org)
    t = _login(api, pw, "valadmin")

    for verb in ("deactivate", "reactivate"):
        assert api.post(_url(org, member, verb), headers=_h(t), json={}).status_code == 422
        assert api.post(_url(org, member, verb), headers=_h(t), json={"reason": ""}).status_code == 422
        assert api.post(_url(org, member, verb), headers=_h(t), json={"reason": "   "}).status_code == 422
        assert api.post(_url(org, member, verb), headers=_h(t), json={"reason": "x" * 1001}).status_code == 422
    assert api.get(_url(org, "not-a-uuid", "status"), headers=_h(t)).status_code == 422

    me = api.post(_url(org, admin, "deactivate"), headers=_h(t), json={"reason": "r"})
    assert me.status_code == 409 and me.json()["error_code"] == "member.cannot_deactivate_self"
    nr = api.post(_url(org, member, "reactivate"), headers=_h(t), json={"reason": "r"})
    assert nr.status_code == 409 and nr.json()["error_code"] == "member.not_deactivated"
    assert api.post(_url(org, member, "deactivate"), headers=_h(t), json={"reason": "r"}).status_code == 200
    again = api.post(_url(org, member, "deactivate"), headers=_h(t), json={"reason": "r"})
    assert again.status_code == 409 and again.json()["error_code"] == "member.already_deactivated"
    assert other_admin


@pytest.mark.asyncio
async def test_only_administrators_of_this_organization_may_use_the_endpoints(pw, api) -> None:  # noqa: F811
    a, b = await _org(pw, "ia"), await _org(pw, "ib")
    admin_a = await _admin(pw, "iadmina", a)
    admin_b = await _admin(pw, "iadminb", b)
    plain = await _user(pw, "iplain", a)
    victim = await _user(pw, "ivictim", a)
    foreign = await _user(pw, "iforeign", b)
    plain_t, admin_b_t = _login(api, pw, "iplain"), _login(api, pw, "iadminb")

    for verb, method in (("status", "get"), ("deactivate", "post"), ("reactivate", "post")):
        kwargs = {"json": {"reason": "r"}} if method == "post" else {}
        # a member without tenancy:manage
        r = getattr(api, method)(_url(a, victim, verb), headers=_h(plain_t), **kwargs)
        assert r.status_code == 403, (verb, r.text)
        # an administrator of ANOTHER organization, naming this one
        r = getattr(api, method)(_url(a, victim, verb), headers=_h(admin_b_t), **kwargs)
        assert r.status_code == 403 and r.json()["error_code"] == "users.cross_organization_denied"
        # nobody at all
        assert getattr(api, method)(_url(a, victim, verb), **kwargs).status_code == 403
    # a member cannot read their OWN status either (the reason is for administrators)
    assert api.get(_url(a, plain, "status"), headers=_h(plain_t)).status_code == 403
    # the foreign administrator naming a user of THEIR organization via THEIR path works only for their own
    # members; naming the other organization's user through their own path is the same 404 as an unknown id
    for target in (victim, uuid.uuid4()):
        r = api.post(_url(b, target, "deactivate"), headers=_h(admin_b_t), json={"reason": "r"})
        assert r.status_code == 404 and r.json()["error_code"] == "member.not_found"
    assert (await pw.sup("SELECT count(*) FROM organization_member_states WHERE organization_id IN (:a,:b)", a=a, b=b))[0][0] == 0
    assert admin_a and admin_b and foreign


# NOTE on the last-administrator guard: over HTTP it cannot be triggered by a different actor, because any
# actor who may call these endpoints holds `tenancy:manage` and is therefore an administrator themselves, so
# the set can never shrink to the target alone; deactivating oneself is refused earlier. The guard is a
# defence in depth against races and legacy data, and is tested directly in
# `tests/core/users/test_member_offboarding_postgres.py` (including how a deactivated admin stops counting).


@pytest.mark.asyncio
async def test_the_audit_trail_records_who_did_what_and_why(pw, api) -> None:  # noqa: F811
    org = await _org(pw, "audit")
    admin = await _admin(pw, "auditadmin", org)
    member = await _user(pw, "auditmember", org)
    t = _login(api, pw, "auditadmin")
    api.post(_url(org, member, "deactivate"), headers=_h(t), json={"reason": "left the company"})
    api.post(_url(org, member, "reactivate"), headers=_h(t), json={"reason": "rehired"})
    entries = api.get(f"/organizations/{org}/audit", headers=_h(t), params={"resource_type": "user"})
    assert entries.status_code == 200, entries.text
    mine = [e for e in entries.json() if e["action"] in ("member.deactivate", "member.reactivate")]
    assert {e["action"] for e in mine} == {"member.deactivate", "member.reactivate"}
    assert all(e["actor"] == f"user:{admin}" and e["resource_id"] == str(member) for e in mine)
    by_action = {e["action"]: e for e in mine}
    assert by_action["member.deactivate"]["event_metadata"]["reason"] == "left the company"
    assert by_action["member.deactivate"]["event_metadata"]["source"] == "admin"
    assert by_action["member.reactivate"]["event_metadata"]["reason"] == "rehired"
    # an ordinary member cannot read the audit trail at all
    plain = await _user(pw, "auditplain", org)
    assert api.get(f"/organizations/{org}/audit", headers=_h(_login(api, pw, "auditplain"))).status_code == 403
    assert plain
