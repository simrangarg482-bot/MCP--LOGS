"""Organization lifecycle APIs over real HTTP (`/platform/organizations/*`): the real FastAPI app and real
PostgreSQL as `ekip_app`. No real email is ever sent (a recording fake sender replaces the provider).

Skipped unless EKIP_TEST_* and DATABASE_URL point at a LOCAL migrated database."""

from __future__ import annotations

import asyncio
import os
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.api.rate_limit as rate_limit_module
from app.api import main as api_main
from app.core.auth import passwords
from app.core.platform import notifications as platform_notifications
from app.shared.config.settings import get_settings
from app.shared.email import EmailDeliveryError
from app.shared.rate_limiter import TokenBucketRateLimiter
from tests.api.test_platform_auth_api_postgres import (  # noqa: F401  (fixtures + helpers)
    _bearer as _bearer_token,
    _dispose_engine,
    _enable,
    _login,
    _operator,
)
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


def _bearer(token):
    """Authorization header from a token string OR a session dict returned by the tenant login."""
    return _bearer_token(token["access_token"] if isinstance(token, dict) else token)


class RecordingSender:
    def __init__(self, fail: bool = False) -> None:
        self.sent = []
        self.fail = fail

    async def send(self, message) -> None:
        if self.fail:
            raise EmailDeliveryError("provider down")
        self.sent.append(message)


@pytest.fixture()
def mail(monkeypatch):
    sender = RecordingSender()
    monkeypatch.setattr(platform_notifications, "get_email_sender", lambda: sender)
    return sender


@pytest.fixture()
def api(monkeypatch, mail):
    monkeypatch.setattr(rate_limit_module, "_limiter", TokenBucketRateLimiter())
    _enable(monkeypatch, True)
    with TestClient(api_main.app, raise_server_exceptions=False) as client:
        yield client
    _dispose_engine()


@pytest_asyncio.fixture(autouse=True)
async def _purge(pw):  # noqa: F811
    if not await pw.sup("SELECT 1 FROM pg_proc WHERE proname='platform_list_organizations'"):
        pytest.skip("run `alembic upgrade head` on this database")
    yield
    like = f"rm-{pw.tag}-%"
    engine = _engine(_SUPER_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL session_replication_role = replica"))
            await conn.execute(
                text("DELETE FROM platform_audit_events WHERE target_organization_id IN (SELECT id FROM organizations WHERE slug LIKE :s) OR actor_user_id IN (SELECT id FROM users WHERE email LIKE :e)"),
                {"s": like, "e": like},
            )
            await conn.execute(
                text("DELETE FROM organization_status_history WHERE organization_id IN (SELECT id FROM organizations WHERE slug LIKE :s)"),
                {"s": like},
            )
    finally:
        await engine.dispose()


async def _admin_user(pw, name, *orgs):  # noqa: F811
    """A tenant admin (holds tenancy:manage) of `orgs`."""
    uid = await _user(pw, name, *orgs)
    for org in orgs:
        await pw.sup(
            "INSERT INTO user_roles (user_id, organization_id, role_id) SELECT :u, :o, id FROM roles WHERE name='admin' AND organization_id IS NULL ON CONFLICT DO NOTHING",
            u=uid, o=org,
        )
    return uid


def _token(api, pw, name):  # noqa: F811
    r = _login(api, pw, name)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _post(api, token, org, verb, **body):
    return api.post(f"/platform/organizations/{org}/{verb}", headers=_bearer(token), json=body)


def _tenant_login(api, pw, name, org=None):  # noqa: F811
    r = api.post("/auth/login", json={"email": pw.email(name), "password": OLD_PW})
    body = r.json()
    if body.get("status") == "organization_selection_required":
        body = api.post(
            "/auth/select-organization", json={"selection_token": body["selection_token"], "organization_id": str(org)}
        ).json()
    return body


# ---------------------------------------------------------------- listing / search / filter / detail


@pytest.mark.asyncio
async def test_list_search_filter_and_page(pw, api) -> None:  # noqa: F811
    await _operator(pw, "lst", role="viewer")
    token = _token(api, pw, "lst")
    a, b, c = await _org(pw, "alpha"), await _org(pw, "beta"), await _org(pw, "gamma")
    await _user(pw, "m1", a, b)
    await _user(pw, "m2", a)
    await pw.sup("UPDATE organizations SET status='active' WHERE id=:o", o=b)
    await pw.sup("UPDATE organizations SET status='suspended' WHERE id=:o", o=c)
    headers = _bearer(token)
    prefix = f"rm-{pw.tag}"

    everything = api.get("/platform/organizations", headers=headers, params={"search": prefix}).json()
    assert everything["total"] == 3 and {i["slug"] for i in everything["items"]} == {f"{prefix}-alpha", f"{prefix}-beta", f"{prefix}-gamma"}
    by_slug = {i["slug"]: i for i in everything["items"]}
    assert by_slug[f"{prefix}-alpha"]["member_count"] == 2 and by_slug[f"{prefix}-beta"]["member_count"] == 1
    for status, slug in (("onboarding", "alpha"), ("active", "beta"), ("suspended", "gamma")):
        only = api.get("/platform/organizations", headers=headers, params={"search": prefix, "status": status}).json()
        assert [i["slug"] for i in only["items"]] == [f"{prefix}-{slug}"] and only["total"] == 1
    assert api.get("/platform/organizations", headers=headers, params={"search": "BETA", "status": "active"}).json()["total"] >= 1
    page = api.get("/platform/organizations", headers=headers, params={"search": prefix, "limit": 2, "offset": 2}).json()
    assert len(page["items"]) == 1 and page["total"] == 3 and page["limit"] == 2 and page["offset"] == 2
    # wildcard characters are data, not patterns
    assert api.get("/platform/organizations", headers=headers, params={"search": "%"}).json()["total"] == 0
    assert api.get("/platform/organizations", headers=headers, params={"search": "_"}).json()["total"] == 0
    # bad input
    assert api.get("/platform/organizations", headers=headers, params={"status": "deleted"}).status_code == 422
    assert api.get("/platform/organizations", headers=headers, params={"limit": 1000}).status_code == 422


@pytest.mark.asyncio
async def test_detail_and_unknown_organization(pw, api) -> None:  # noqa: F811
    await _operator(pw, "det", role="viewer")
    token = _token(api, pw, "det")
    org = await _org(pw, "det")
    detail = api.get(f"/platform/organizations/{org}", headers=_bearer(token)).json()
    assert detail["id"] == str(org) and detail["status"] == "onboarding" and detail["session_epoch"] == 0
    assert detail["status_message_public"] is None and detail["history_count"] == 0
    missing = api.get(f"/platform/organizations/{uuid.uuid4()}", headers=_bearer(token))
    assert missing.status_code == 404 and missing.json()["error_code"] == "organization.not_found"
    assert api.get(f"/platform/organizations/{uuid.uuid4()}/status-history", headers=_bearer(token)).status_code == 404


# ---------------------------------------------------------------- the whole lifecycle


@pytest.mark.asyncio
async def test_activate_suspend_reactivate_end_to_end(pw, api, mail) -> None:  # noqa: F811
    org, other = await _org(pw, "life"), await _org(pw, "other")
    admin = await _admin_user(pw, "admin1", org, other)
    await _admin_user(pw, "admin2", org)
    member = await _user(pw, "member", org)  # not an administrator
    await _operator(pw, "ops")
    token = _token(api, pw, "ops")

    in_org = _tenant_login(api, pw, "admin1", org)
    in_other = _tenant_login(api, pw, "admin1", other)
    assert api.get("/auth/me", headers=_bearer(in_org)).status_code == 200

    # activate
    r = _post(api, token, org, "activate", reason="verified by phone")
    assert r.status_code == 200 and r.json()["new_status"] == "active" and r.json()["previous_status"] == "onboarding"
    assert r.json()["organization"]["session_epoch"] == 0
    assert api.get("/auth/me", headers=_bearer(in_org)).status_code == 200  # sessions survive an activation
    assert mail.sent == []  # activation sends nothing

    # suspend, with a public message and a private note
    r = _post(api, token, org, "suspend", reason="PRIVATE-REASON-XYZ", customer_message="Please contact billing", internal_note="PRIVATE-NOTE-XYZ")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["new_status"] == "suspended" and body["organization"]["status_message_public"] == "Please contact billing"
    assert body["organization"]["session_epoch"] == 1 and body["notification_queued"] is True
    assert "PRIVATE-NOTE-XYZ" not in r.text and "PRIVATE-REASON-XYZ" not in r.text  # not even echoed back

    # the organization is shut out everywhere, scoped to it alone
    refused = api.get("/auth/me", headers=_bearer(in_org))
    assert refused.status_code == 403 and refused.json()["error_code"] == "auth.organization_suspended"
    assert refused.json()["detail"]["customer_message"] == "Please contact billing"
    assert set(refused.json()["detail"]) == {"organization_name", "customer_message"}  # nothing else leaks
    assert api.post("/auth/refresh", json={"refresh_token": in_org["refresh_token"]}).json()["error_code"] == "auth.organization_suspended"
    assert api.get("/auth/me", headers=_bearer(in_other)).status_code == 200  # the SAME admin's other organization
    assert api.post("/auth/refresh", json={"refresh_token": in_other["refresh_token"]}).status_code == 200
    login = api.post("/auth/login", json={"email": pw.email("member"), "password": OLD_PW})
    assert login.status_code == 403 and login.json()["detail"]["customer_message"] == "Please contact billing"

    # emails: the organization's administrators only, public facts only
    assert sorted(m.to for m in mail.sent) == sorted([pw.email("admin1"), pw.email("admin2")])
    for m in mail.sent:
        blob = m.subject + m.text + (m.html or "")
        assert "Please contact billing" in blob and "PRIVATE" not in blob and "reason" not in blob.lower().replace("reasonable", "")
    assert member and admin
    mail.sent.clear()

    # reactivate
    r = _post(api, token, org, "reactivate", reason="paid")
    assert r.status_code == 200 and r.json()["new_status"] == "active" and r.json()["organization"]["session_epoch"] == 2
    assert r.json()["organization"]["status_message_public"] is None
    stale = api.get("/auth/me", headers=_bearer(in_org))
    assert stale.status_code == 403 and stale.json()["error_code"] == "auth.session_revoked"  # old tokens stay dead
    assert api.post("/auth/refresh", json={"refresh_token": in_org["refresh_token"]}).status_code == 403
    fresh = _tenant_login(api, pw, "admin1", org)
    assert api.get("/auth/me", headers=_bearer(fresh)).status_code == 200  # signing in again works
    assert api.get("/auth/me", headers=_bearer(in_other)).status_code == 200
    assert len(mail.sent) == 2 and all("restored" in m.subject for m in mail.sent) and all("PRIVATE" not in m.text for m in mail.sent)

    # history: newest first, with the private fields visible to operators only
    history = api.get(f"/platform/organizations/{org}/status-history", headers=_bearer(token)).json()
    assert history["total"] == 3
    assert [(h["previous_status"], h["new_status"]) for h in history["items"]] == [
        ("suspended", "active"), ("active", "suspended"), ("onboarding", "active")
    ]
    suspension = history["items"][1]
    assert suspension["reason"] == "PRIVATE-REASON-XYZ" and suspension["internal_note"] == "PRIVATE-NOTE-XYZ"
    assert suspension["customer_message"] == "Please contact billing" and suspension["operator_email"] == pw.email("ops")

    # platform audit + tenant-visible audit
    assert (await pw.sup("SELECT count(*) FROM platform_audit_events WHERE target_organization_id=:o AND action='organization.status_changed'", o=org))[0][0] == 3
    tenant_audit = await pw.sup("SELECT action, metadata::text FROM audit_logs WHERE organization_id=:o AND action LIKE 'organization.%' ORDER BY occurred_at", o=org)
    assert [a for a, _m in tenant_audit] == ["organization.activated", "organization.suspended", "organization.reactivated"]
    assert all("PRIVATE" not in m for _a, m in tenant_audit)
    assert (await pw.sup("SELECT count(*) FROM audit_logs WHERE organization_id=:o AND metadata::text LIKE '%PRIVATE%'", o=org))[0][0] == 0


@pytest.mark.asyncio
async def test_a_tenant_admin_sees_no_internal_data_through_any_tenant_api(pw, api) -> None:  # noqa: F811
    org = await _org(pw, "priv")
    await _admin_user(pw, "tadmin", org)
    await _operator(pw, "ops2")
    token = _token(api, pw, "ops2")
    assert _post(api, token, org, "suspend", reason="PRIV-R", customer_message="public text", internal_note="PRIV-N").status_code == 200
    assert _post(api, token, org, "reactivate", reason="PRIV-R2").status_code == 200
    tenant = _tenant_login(api, pw, "tadmin", org)
    seen = [
        api.get(path, headers=_bearer(tenant)).text
        for path in (f"/organizations/{org}", "/auth/me", "/auth/organizations", f"/organizations/{org}/projects")
    ]
    assert "PRIV-" not in " ".join(seen)
    # and the tenant API offers no way to change a status
    for method, path in (("patch", f"/organizations/{org}"), ("put", f"/organizations/{org}"), ("delete", f"/organizations/{org}")):
        assert api.request(method.upper(), path, headers=_bearer(tenant), json={"status": "suspended"}).status_code in (404, 405, 403)
    assert (await pw.sup("SELECT status FROM organizations WHERE id=:o", o=org))[0][0] == "active"


# ---------------------------------------------------------------- invalid transitions and input


@pytest.mark.asyncio
async def test_invalid_transitions_are_rejected_clearly_and_change_nothing(pw, api) -> None:  # noqa: F811
    org = await _org(pw, "inv")
    await _operator(pw, "opsinv")
    token = _token(api, pw, "opsinv")

    def expect(verb, code, **body):
        r = _post(api, token, org, verb, **{"reason": "r", **body})
        assert r.status_code == 409 and r.json()["error_code"] == code, (verb, r.status_code, r.text)

    expect("reactivate", "organization.invalid_transition")  # onboarding cannot be reactivated
    assert _post(api, token, org, "activate", reason="r").status_code == 200
    expect("activate", "organization.already_in_status")  # a repeat is an error, never silently accepted
    expect("reactivate", "organization.already_in_status")  # already active: nothing to reactivate
    assert _post(api, token, org, "suspend", reason="r").status_code == 200
    expect("suspend", "organization.already_in_status")
    expect("activate", "organization.invalid_transition")  # a suspended organization is reactivated, not activated
    assert (await pw.sup("SELECT count(*) FROM organization_status_history WHERE organization_id=:o", o=org))[0][0] == 2
    assert (await pw.sup("SELECT session_epoch FROM organizations WHERE id=:o", o=org))[0][0] == 1


@pytest.mark.asyncio
async def test_a_reason_is_required_and_lengths_are_bounded(pw, api) -> None:  # noqa: F811
    org = await _org(pw, "val")
    await _operator(pw, "opsval")
    token = _token(api, pw, "opsval")
    for verb in ("activate", "suspend", "reactivate"):
        assert api.post(f"/platform/organizations/{org}/{verb}", headers=_bearer(token), json={}).status_code == 422
        assert _post(api, token, org, verb, reason="").status_code == 422
    assert _post(api, token, org, "suspend", reason="   ").status_code in (400, 422)  # blank after trimming
    assert _post(api, token, org, "suspend", reason="x" * 1001).status_code == 422
    assert _post(api, token, org, "suspend", reason="r", customer_message="m" * 1001).status_code == 422
    assert _post(api, token, org, "suspend", reason="r", internal_note="n" * 4001).status_code == 422
    assert (await pw.sup("SELECT status FROM organizations WHERE id=:o", o=org))[0][0] == "onboarding"
    assert _post(api, token, uuid.uuid4(), "suspend", reason="r").status_code == 404


# ---------------------------------------------------------------- authorization


@pytest.mark.asyncio
async def test_only_platform_admins_may_change_a_status_and_tenant_credentials_are_useless(pw, api) -> None:  # noqa: F811
    org = await _org(pw, "authz")
    tenant_admin = await _admin_user(pw, "tadm", org)
    await _operator(pw, "viewer1", role="viewer")
    await _operator(pw, "admin1x")
    viewer, admin = _token(api, pw, "viewer1"), _token(api, pw, "admin1x")
    tenant = _tenant_login(api, pw, "tadm", org)["access_token"]

    for verb, body in (("activate", {"reason": "r"}), ("suspend", {"reason": "r"}), ("reactivate", {"reason": "r"})):
        r = _post(api, viewer, org, verb, **body)
        assert r.status_code == 403 and r.json()["error_code"] == "platform.admin_required"
        assert _post(api, tenant, org, verb, **body).status_code == 403  # a tenant admin (tenancy:manage)
        assert _post(api, "garbage", org, verb, **body).status_code == 403
    for path in ("/platform/organizations", f"/platform/organizations/{org}", f"/platform/organizations/{org}/status-history"):
        assert api.get(path, headers=_bearer(tenant)).status_code == 403
        assert api.get(path).status_code == 403
        assert api.get(path, headers=_bearer(viewer)).status_code == 200  # a viewer may read
    assert (await pw.sup("SELECT status FROM organizations WHERE id=:o", o=org))[0][0] == "onboarding"
    assert tenant_admin and admin


@pytest.mark.asyncio
async def test_a_revoked_operator_cannot_transition_and_the_kill_switch_blocks_everything(pw, api, monkeypatch) -> None:  # noqa: F811
    org = await _org(pw, "ks")
    uid = await _operator(pw, "opsks")
    token = _token(api, pw, "opsks")
    await pw.sup("UPDATE platform_operators SET revoked_at=now() WHERE user_id=:u", u=uid)
    assert _post(api, token, org, "suspend", reason="r").status_code == 403
    assert (await pw.sup("SELECT status FROM organizations WHERE id=:o", o=org))[0][0] == "onboarding"
    await pw.sup("UPDATE platform_operators SET revoked_at=NULL WHERE user_id=:u", u=uid)
    assert _post(api, token, org, "suspend", reason="r").status_code == 200
    _enable(monkeypatch, False)
    for r in (
        _post(api, token, org, "reactivate", reason="r"),
        api.get("/platform/organizations", headers=_bearer(token)),
        api.get(f"/platform/organizations/{org}/status-history", headers=_bearer(token)),
    ):
        assert r.status_code == 404 and r.json()["error_code"] == "platform.unavailable"
    assert (await pw.sup("SELECT status FROM organizations WHERE id=:o", o=org))[0][0] == "suspended"


# ---------------------------------------------------------------- concurrency


@pytest.mark.asyncio
async def test_concurrent_suspensions_exactly_one_wins(pw, monkeypatch, mail) -> None:  # noqa: F811
    monkeypatch.setattr(rate_limit_module, "_limiter", TokenBucketRateLimiter())
    _enable(monkeypatch, True)
    org = await _org(pw, "race")
    await _admin_user(pw, "raceadmin", org)
    await _operator(pw, "opsrace")
    _dispose_engine()
    transport = httpx.ASGITransport(app=api_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        login = await client.post("/platform/auth/login", json={"email": pw.email("opsrace"), "password": OLD_PW})
        headers = _bearer(login.json()["access_token"])
        results = await asyncio.gather(
            *[client.post(f"/platform/organizations/{org}/suspend", headers=headers, json={"reason": f"r{i}"}) for i in range(5)]
        )
        codes = sorted(r.status_code for r in results)
        assert codes == [200, 409, 409, 409, 409], [r.text for r in results]
        assert {r.json()["error_code"] for r in results if r.status_code == 409} == {"organization.already_in_status"}
        # reactivate racing with a suspend
        mixed = await asyncio.gather(
            client.post(f"/platform/organizations/{org}/reactivate", headers=headers, json={"reason": "r"}),
            client.post(f"/platform/organizations/{org}/reactivate", headers=headers, json={"reason": "r"}),
        )
        assert sorted(r.status_code for r in mixed) == [200, 409]
    _dispose_engine()
    assert (await pw.sup("SELECT count(*) FROM organization_status_history WHERE organization_id=:o", o=org))[0][0] == 2
    assert (await pw.sup("SELECT session_epoch, status FROM organizations WHERE id=:o", o=org))[0] == (2, "active")
    assert len(mail.sent) == 2  # the one administrator: one suspension notice + one reactivation notice


# ---------------------------------------------------------------- multi-organization isolation


@pytest.mark.asyncio
async def test_suspending_one_organization_leaves_every_other_organization_alone(pw, api) -> None:  # noqa: F811
    a, b, c = await _org(pw, "isoa"), await _org(pw, "isob"), await _org(pw, "isoc")
    await _user(pw, "shared", a, b)
    await _user(pw, "onlyc", c)
    await _operator(pw, "opsiso")
    token = _token(api, pw, "opsiso")
    in_a, in_b = _tenant_login(api, pw, "shared", a), _tenant_login(api, pw, "shared", b)
    in_c = _tenant_login(api, pw, "onlyc", c)
    before_b = (await pw.sup("SELECT count(*) FROM refresh_tokens WHERE organization_id=:o AND revoked_at IS NULL", o=b))[0][0]

    assert _post(api, token, a, "suspend", reason="r").status_code == 200
    assert (await pw.sup("SELECT count(*) FROM refresh_tokens WHERE organization_id=:o AND revoked_at IS NULL", o=a))[0][0] == 0
    assert (await pw.sup("SELECT count(*) FROM refresh_tokens WHERE organization_id=:o AND revoked_at IS NULL", o=b))[0][0] == before_b
    assert (await pw.sup("SELECT session_epoch FROM organizations WHERE id IN (:b,:c)", b=b, c=c)) == [(0,), (0,)]
    assert api.get("/auth/me", headers=_bearer(in_a)).status_code == 403
    for tokens in (in_b, in_c):
        assert api.get("/auth/me", headers=_bearer(tokens)).status_code == 200
        assert api.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 200


# ---------------------------------------------------------------- notification failure handling


@pytest.mark.asyncio
async def test_an_email_failure_never_undoes_the_status_change(pw, api, monkeypatch) -> None:  # noqa: F811
    failing = RecordingSender(fail=True)
    monkeypatch.setattr(platform_notifications, "get_email_sender", lambda: failing)
    monkeypatch.setattr(platform_notifications, "_DELIVERY_ATTEMPTS", 1)
    org = await _org(pw, "mailfail")
    await _admin_user(pw, "mfadmin", org)
    await _operator(pw, "opsmf")
    token = _token(api, pw, "opsmf")
    r = _post(api, token, org, "suspend", reason="r", customer_message="m")
    assert r.status_code == 200 and r.json()["notification_queued"] is True
    assert (await pw.sup("SELECT status FROM organizations WHERE id=:o", o=org))[0][0] == "suspended"
    assert (await pw.sup("SELECT count(*) FROM organization_status_history WHERE organization_id=:o", o=org))[0][0] == 1


@pytest.mark.asyncio
async def test_a_failure_while_choosing_recipients_never_undoes_the_status_change(pw, api, monkeypatch) -> None:  # noqa: F811
    async def boom(*_a, **_k):
        raise RuntimeError("recipient lookup exploded")

    monkeypatch.setattr(platform_notifications, "prepare_status_notices", boom)
    org = await _org(pw, "prepfail")
    await _operator(pw, "opspf")
    token = _token(api, pw, "opspf")
    r = _post(api, token, org, "suspend", reason="r")
    assert r.status_code == 200 and r.json()["notification_queued"] is False
    assert (await pw.sup("SELECT status FROM organizations WHERE id=:o", o=org))[0][0] == "suspended"


@pytest.mark.asyncio
async def test_no_provider_configured_is_not_an_error(pw, api, monkeypatch) -> None:  # noqa: F811
    from app.shared.email import EmailNotConfiguredError

    def not_configured():
        raise EmailNotConfiguredError("none")

    monkeypatch.setattr(platform_notifications, "get_email_sender", not_configured)
    org = await _org(pw, "nomail")
    await _admin_user(pw, "nmadmin", org)
    await _operator(pw, "opsnm")
    token = _token(api, pw, "opsnm")
    assert _post(api, token, org, "suspend", reason="r").status_code == 200


# ---------------------------------------------------------------- activation policy


@pytest.mark.asyncio
async def test_self_service_signup_becomes_active_after_verification_but_other_organizations_do_not(pw, monkeypatch) -> None:  # noqa: F811
    from app.core.auth import email_verification
    from app.core.tenancy import service as tenancy_service
    from app.core.tenancy.schemas import OrganizationCreate

    delivery = await pw.run(
        None,
        lambda s: email_verification.start_registration(
            s, email=pw.email("founder"), password_hash=passwords.hash_password(OLD_PW), display_name="Founder",
            organization_name="Founders Inc", organization_slug=f"rm-{pw.tag}-founders",
        ),
    )
    await pw.run(None, lambda s: email_verification.verify_token(s, delivery.raw_token))
    assert (await pw.sup("SELECT status FROM organizations WHERE slug=:s", s=f"rm-{pw.tag}-founders"))[0][0] == "active"
    # the founder can sign in immediately
    from app.core.auth import service as auth_service
    from app.core.auth.schemas import LoginRequest

    login = await pw.run(None, lambda s: auth_service.login_with_password(s, LoginRequest(email=pw.email("founder"), password=OLD_PW)))
    assert login.status == "complete"
    # an organization created any other way stays onboarding until an operator activates it
    extra = await pw.run(None, lambda s: tenancy_service.create_organization(s, OrganizationCreate(name="Extra", slug=f"rm-{pw.tag}-extra")))
    assert extra.status == "onboarding"
