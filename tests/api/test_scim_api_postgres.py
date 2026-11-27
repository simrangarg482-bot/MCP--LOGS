"""SCIM 2.0 over real HTTP: the real FastAPI app, real PostgreSQL as `ekip_app`, simulated Entra ID and Okta
requests. No identity provider and no email is involved.

Skipped unless EKIP_TEST_* and DATABASE_URL point at a LOCAL migrated database."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import app.api.rate_limit as rate_limit_module
from app.api import main as api_main
from app.api.routers import scim as scim_router
from app.core.scim import admin as scim_admin
from app.core.scim import tokens
from app.shared.config.settings import get_settings
from app.shared.rate_limiter import TokenBucketRateLimiter
from tests.api.test_member_offboarding_api_postgres import _admin, _h, _login  # noqa: F401  (helpers)
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

PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
USER = "urn:ietf:params:scim:schemas:core:2.0:User"
MEDIA = "application/scim+json"


def _enable(monkeypatch, on: bool = True, **overrides):
    settings = get_settings().model_copy(update={"scim_enabled": on, **overrides})
    monkeypatch.setattr(scim_router, "get_settings", lambda: settings)
    monkeypatch.setattr(tokens, "get_settings", lambda: settings)
    monkeypatch.setattr(scim_admin, "get_settings", lambda: settings)
    return settings


@pytest.fixture()
def api(monkeypatch):
    monkeypatch.setattr(rate_limit_module, "_limiter", TokenBucketRateLimiter())
    _enable(monkeypatch)
    with TestClient(api_main.app, raise_server_exceptions=False) as client:
        yield client
    from app.database import session as db_session

    db_session.engine.sync_engine.dispose(close=False)


@pytest_asyncio.fixture(autouse=True)
async def _migrated(pw):  # noqa: F811
    if not await pw.sup("SELECT 1 FROM information_schema.tables WHERE table_name='scim_identities'"):
        pytest.skip("run `alembic upgrade head` on this database")
    yield


async def _scim_org(pw, name, *, enabled=True, settle=300, guard_min=5, guard_percent=20):  # noqa: F811
    """An organization with SCIM configured and one valid token. Returns (org_id, raw_token)."""
    org = await _org(pw, name)
    await pw.sup(
        "INSERT INTO scim_configurations (organization_id, enabled, guard_min_absolute, guard_percent, reactivation_settle_seconds) "
        "VALUES (:o, :e, :gm, :gp, :s)", o=org, e=enabled, gm=guard_min, gp=guard_percent, s=settle,
    )
    return org, await _token(pw, org)


async def _token(pw, org, *, expires_in=timedelta(days=30), revoked=False):  # noqa: F811
    raw = tokens.generate_raw_token()
    await pw.sup(
        "INSERT INTO scim_tokens (organization_id, name, token_hash, token_prefix, expires_at, revoked_at) VALUES (:o,'t',:h,:p,:x,:r)",
        o=org, h=tokens.hash_token(raw), p=raw[:12], x=datetime.now(UTC) + expires_in, r=datetime.now(UTC) if revoked else None,
    )
    return raw


def _a(raw):
    return {"Authorization": f"Bearer {raw}", "Content-Type": MEDIA}


def _create(api, raw, user_name, **extra):
    return api.post("/scim/v2/Users", headers=_a(raw), json={"schemas": [USER], "userName": user_name, **extra})


def _entra_deactivate(api, raw, ident):
    return api.patch(
        f"/scim/v2/Users/{ident}", headers=_a(raw),
        json={"schemas": [PATCH], "Operations": [{"op": "Replace", "path": "active", "value": "False"}]},
    )


async def _linked_member(pw, api, org, raw, name):  # noqa: F811
    """A member linked to a SCIM identity by PROOF (the externalId equals the subject EKIP verified via OIDC)."""
    uid = await _user(pw, name, org)
    subject = f"sub-{uuid.uuid4().hex[:10]}"
    await pw.sup("INSERT INTO external_identity_mappings (organization_id, user_id, idp_subject) VALUES (:o,:u,:s)", o=org, u=uid, s=subject)
    r = _create(api, raw, pw.email(name), externalId=subject)
    assert r.status_code == 201, r.text
    return uid, r.json()["id"]


async def _state(pw, org, user):  # noqa: F811
    rows = await pw.sup("SELECT status, source, member_epoch FROM organization_member_states WHERE organization_id=:o AND user_id=:u", o=org, u=user)
    return rows[0] if rows else None


# ================================================================ kill switch, authentication, isolation


@pytest.mark.asyncio
async def test_disabled_by_default_every_scim_route_answers_a_scim_404(pw, api, monkeypatch) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "off")
    _enable(monkeypatch, on=False)
    assert get_settings().scim_enabled is False  # the shipped default
    for method, path in (("get", "/scim/v2/Users"), ("post", "/scim/v2/Users"), ("get", "/scim/v2/ServiceProviderConfig"), ("delete", f"/scim/v2/Users/{uuid.uuid4()}")):
        r = getattr(api, method)(path, headers=_a(raw))
        assert r.status_code == 404 and r.headers["content-type"].startswith(MEDIA)
        assert r.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"] and r.json()["status"] == "404"


@pytest.mark.asyncio
async def test_authentication_failures_are_one_identical_401(pw, api) -> None:  # noqa: F811
    org, good = await _scim_org(pw, "auth")
    expired = await _token(pw, org, expires_in=timedelta(seconds=-5))
    revoked = await _token(pw, org, revoked=True)
    admin = await _admin(pw, "authadmin", org)
    jwt = _login(api, pw, "authadmin")["access_token"]
    answers = set()
    for header in (
        {}, {"Authorization": "Bearer"}, {"Authorization": "Basic abc"}, {"Authorization": f"Bearer {expired}"},
        {"Authorization": f"Bearer {revoked}"}, {"Authorization": f"Bearer {jwt}"},
        {"Authorization": "Bearer scim_does-not-exist"}, {"Authorization": f"Bearer {good}x"},
    ):
        r = api.get("/scim/v2/Users", headers=header)
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer"), header
        answers.add(r.json()["detail"])
    assert len(answers) <= 2  # "a bearer token is required" for a missing header, one sentence for everything else
    assert api.get("/scim/v2/Users", headers=_a(good)).status_code == 200
    assert admin


@pytest.mark.asyncio
async def test_a_scim_token_authenticates_nothing_else_and_a_tenant_token_nothing_here(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "sep")
    await _admin(pw, "sepadmin", org)
    tenant = _login(api, pw, "sepadmin")
    for path in ("/auth/me", f"/organizations/{org}/projects", "/incidents", f"/organizations/{org}/scim"):
        r = api.get(path, headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code in (401, 403), path
        assert r.json()["error_code"] == "auth.invalid_token", path
    assert api.get("/scim/v2/Users", headers=_h(tenant)).status_code == 401  # a real access token is no SCIM credential


@pytest.mark.asyncio
async def test_a_disabled_or_suspended_organization_is_refused(pw, api) -> None:  # noqa: F811
    off, raw_off = await _scim_org(pw, "orgoff", enabled=False)
    r = api.get("/scim/v2/Users", headers=_a(raw_off))
    assert r.status_code == 403 and "disabled" in r.json()["detail"]
    live, raw = await _scim_org(pw, "orgsusp")
    assert api.get("/scim/v2/Users", headers=_a(raw)).status_code == 200
    await pw.sup("UPDATE organizations SET status='suspended' WHERE id=:o", o=live)
    r = api.get("/scim/v2/Users", headers=_a(raw))
    assert r.status_code == 403 and "not available" in r.json()["detail"]
    assert api.post("/scim/v2/Users", headers=_a(raw), json={"userName": "x@example.test"}).status_code == 403


@pytest.mark.asyncio
async def test_a_token_only_ever_sees_its_own_organization(pw, api) -> None:  # noqa: F811
    a, raw_a = await _scim_org(pw, "isoa")
    b, raw_b = await _scim_org(pw, "isob")
    uid_b, ident_b = await _linked_member(pw, api, b, raw_b, "isomemb")
    _create(api, raw_a, "only-in-a@example.test")

    listing = api.get("/scim/v2/Users", headers=_a(raw_a)).json()
    assert [r["userName"] for r in listing["Resources"]] == ["only-in-a@example.test"]
    assert api.get(f"/scim/v2/Users/{ident_b}", headers=_a(raw_a)).status_code == 404
    assert api.patch(f"/scim/v2/Users/{ident_b}", headers=_a(raw_a), json={"Operations": [{"op": "replace", "path": "active", "value": False}]}).status_code == 404
    assert api.delete(f"/scim/v2/Users/{ident_b}", headers=_a(raw_a)).status_code == 404
    assert api.put(f"/scim/v2/Users/{ident_b}", headers=_a(raw_a), json={"active": False}).status_code == 404
    assert await _state(pw, b, uid_b) is None  # B's member is untouched
    # the same userName may exist independently in two organizations
    assert _create(api, raw_b, "only-in-a@example.test").status_code == 201


# ================================================================ discovery and unsupported operations


@pytest.mark.asyncio
async def test_discovery_endpoints(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "disc")
    spc = api.get("/scim/v2/ServiceProviderConfig", headers=_a(raw))
    assert spc.status_code == 200 and spc.headers["content-type"].startswith(MEDIA)
    assert spc.json()["patch"]["supported"] is True and spc.json()["bulk"]["supported"] is False
    schemas_ = api.get("/scim/v2/Schemas", headers=_a(raw)).json()
    assert schemas_["totalResults"] == 2
    assert api.get(f"/scim/v2/Schemas/{USER}", headers=_a(raw)).json()["name"] == "User"
    assert api.get("/scim/v2/Schemas/urn:nope", headers=_a(raw)).status_code == 404
    types = api.get("/scim/v2/ResourceTypes", headers=_a(raw)).json()
    assert {t["id"] for t in types["Resources"]} == {"User", "Group"}
    assert api.get("/scim/v2/ResourceTypes/User", headers=_a(raw)).json()["endpoint"] == "/Users"
    assert api.get("/scim/v2/ResourceTypes/Nope", headers=_a(raw)).status_code == 404


@pytest.mark.asyncio
async def test_unsupported_operations_are_clearly_refused(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "unsup")
    for method in ("get", "post"):
        r = getattr(api, method)("/scim/v2/Bulk", headers=_a(raw))
        assert r.status_code == 501 and r.json()["status"] == "501"
    assert api.get("/scim/v2/Me", headers=_a(raw)).status_code == 501
    assert api.get("/scim/v2/Users?sortBy=userName", headers=_a(raw)).status_code == 400
    bad = api.get("/scim/v2/Users", headers=_a(raw), params={"filter": 'displayName co "x"'})
    assert bad.status_code == 400 and bad.json()["scimType"] == "invalidFilter"
    assert api.get("/scim/v2/Users?startIndex=abc", headers=_a(raw)).status_code == 400
    garbled = api.post("/scim/v2/Users", headers=_a(raw), content=b"{not json")
    assert garbled.status_code == 400 and garbled.json()["scimType"] == "invalidSyntax"
    assert api.post("/scim/v2/Users", headers=_a(raw), content=b"x" * 1_100_000).status_code == 413
    pwd = _create(api, raw, "pw@example.test", password="hunter2")
    assert pwd.status_code == 400 and pwd.json()["scimType"] == "mutability"
    assert _create(api, raw, "").status_code == 400
    assert api.post("/scim/v2/Users", headers=_a(raw), json={"schemas": [USER]}).status_code == 400  # no userName


# ================================================================ users: lookup and creation (no accounts, no grants)


@pytest.mark.asyncio
async def test_creating_a_user_creates_a_pending_identity_not_an_account_and_grants_nothing(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "create")
    users_before = (await pw.sup("SELECT count(*) FROM users"))[0][0]
    roles_before = (await pw.sup("SELECT count(*) FROM user_roles WHERE organization_id=:o", o=org))[0][0]

    r = _create(api, raw, "New.Person@Example.test", externalId="00u-new", displayName="New Person")
    assert r.status_code == 201 and r.headers["content-type"].startswith(MEDIA)
    body = r.json()
    assert body["userName"] == "New.Person@Example.test" and body["externalId"] == "00u-new" and body["active"] is True
    assert body["meta"]["location"].endswith(f"/scim/v2/Users/{body['id']}")

    assert (await pw.sup("SELECT count(*) FROM users"))[0][0] == users_before  # no EKIP account
    assert (await pw.sup("SELECT count(*) FROM user_roles WHERE organization_id=:o", o=org))[0][0] == roles_before
    row = (await pw.sup("SELECT user_id, linked_via FROM scim_identities WHERE id=:i", i=uuid.UUID(body["id"])))[0]
    assert row == (None, None)

    dup = _create(api, raw, "new.person@example.test")  # userName is unique per organization, case-insensitively
    assert dup.status_code == 409 and dup.json()["scimType"] == "uniqueness"
    assert _create(api, raw, "other@example.test", externalId="00u-new").status_code == 409


@pytest.mark.asyncio
async def test_lookup_by_filter_paging_and_single_get(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "look")
    ids = {}
    for i in range(5):
        ids[i] = _create(api, raw, f"user{i}@example.test", externalId=f"ext-{i}").json()["id"]

    one = api.get("/scim/v2/Users", headers=_a(raw), params={"filter": 'userName eq "USER3@example.test"'}).json()
    assert one["totalResults"] == 1 and one["Resources"][0]["id"] == ids[3]
    ext = api.get("/scim/v2/Users", headers=_a(raw), params={"filter": 'externalId eq "ext-4"'}).json()
    assert [r["id"] for r in ext["Resources"]] == [ids[4]]
    assert api.get("/scim/v2/Users", headers=_a(raw), params={"filter": 'userName eq "nobody@example.test"'}).json()["totalResults"] == 0
    page = api.get("/scim/v2/Users?startIndex=2&count=2", headers=_a(raw)).json()
    assert (page["totalResults"], page["startIndex"], page["itemsPerPage"]) == (5, 2, 2)
    probe = api.get("/scim/v2/Users?startIndex=1&count=0", headers=_a(raw)).json()  # Okta's probe
    assert probe["totalResults"] == 5 and probe["Resources"] == []
    assert api.get(f"/scim/v2/Users/{ids[1]}", headers=_a(raw)).json()["userName"] == "user1@example.test"
    assert api.get(f"/scim/v2/Users/{uuid.uuid4()}", headers=_a(raw)).status_code == 404
    assert api.get("/scim/v2/Users/not-a-uuid", headers=_a(raw)).status_code == 404


# ================================================================ identity linking: never by email alone


@pytest.mark.asyncio
async def test_a_matching_email_never_links_and_never_deactivates(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "email")
    member = await _user(pw, "emailmember", org)
    r = _create(api, raw, pw.email("emailmember"), externalId="00u-no-proof")  # same email, no proof
    assert r.status_code == 201
    ident = r.json()["id"]
    assert (await pw.sup("SELECT user_id FROM scim_identities WHERE id=:i", i=uuid.UUID(ident)))[0][0] is None
    assert _entra_deactivate(api, raw, ident).status_code == 200  # recorded...
    assert await _state(pw, org, member) is None  # ...but the member was NOT touched
    assert api.get(f"/scim/v2/Users/{ident}", headers=_a(raw)).json()["active"] is False  # the IdP's own view of a pending identity
    # also with no externalId at all, and with an externalId matching ANOTHER member's subject in another org
    other_org, other_raw = await _scim_org(pw, "emailother")
    other_member = await _user(pw, "emailother-m", other_org)
    await pw.sup("INSERT INTO external_identity_mappings (organization_id, user_id, idp_subject) VALUES (:o,:u,'shared-subject')", o=other_org, u=other_member)
    cross = _create(api, raw, "someone@example.test", externalId="shared-subject")
    assert (await pw.sup("SELECT user_id FROM scim_identities WHERE id=:i", i=uuid.UUID(cross.json()["id"])))[0][0] is None
    assert other_raw


@pytest.mark.asyncio
async def test_a_proven_externalid_links_the_identity_to_the_member(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "proof")
    uid, ident = await _linked_member(pw, api, org, raw, "proofmember")
    row = (await pw.sup("SELECT user_id, linked_via FROM scim_identities WHERE id=:i", i=uuid.UUID(ident)))[0]
    assert row == (uid, "sso_subject")
    audit = await pw.sup("SELECT metadata->>'via' FROM audit_logs WHERE organization_id=:o AND action='scim.identity_linked'", o=org)
    assert audit == [("sso_subject",)]


# ================================================================ deactivation


@pytest.mark.asyncio
async def test_entra_deactivation_cuts_that_organizations_access_and_nothing_else(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "deact")
    other = await _org(pw, "deactother")
    await _admin(pw, "deactadmin", org)
    uid, ident = await _linked_member(pw, api, org, raw, "deactmember")
    await pw.sup(
        "INSERT INTO user_roles (user_id, organization_id, role_id) SELECT :u, :o, id FROM roles WHERE name='viewer' AND organization_id=:o",
        u=uid, o=other,
    )
    in_org, in_other = _login(api, pw, "deactmember", org), _login(api, pw, "deactmember", other)
    assert api.get("/auth/me", headers=_h(in_org)).status_code == 200

    r = _entra_deactivate(api, raw, ident)
    assert r.status_code == 200 and r.json()["active"] is False

    assert await _state(pw, org, uid) == ("deactivated", "scim", 1)
    assert (await pw.sup("SELECT is_active FROM users WHERE id=:u", u=uid))[0][0] is True  # NEVER the global flag
    assert (await pw.sup("SELECT count(*) FROM user_roles WHERE organization_id=:o AND user_id=:u", o=org, u=uid))[0][0] >= 1  # roles kept
    refused = api.get("/auth/me", headers=_h(in_org))
    assert refused.status_code == 403 and refused.json()["error_code"] == "auth.membership_deactivated"
    assert api.post("/auth/refresh", json={"refresh_token": in_org["refresh_token"]}).json()["error_code"] == "auth.membership_deactivated"
    assert api.get("/auth/me", headers=_h(in_other)).status_code == 200  # the same person's other organization
    assert api.post("/auth/refresh", json={"refresh_token": in_other["refresh_token"]}).status_code == 200

    audit = await pw.sup(
        "SELECT actor, metadata->>'source', metadata->>'scim_identity_id' FROM audit_logs WHERE organization_id=:o AND action='member.deactivate'", o=org
    )
    assert len(audit) == 1 and audit[0][0].startswith("agent:scim:") and audit[0][1] == "scim" and audit[0][2] == ident

    # idempotent: a duplicate delivery changes nothing and writes no second event
    again = _entra_deactivate(api, raw, ident)
    assert again.status_code == 200 and again.json()["active"] is False
    assert await _state(pw, org, uid) == ("deactivated", "scim", 1)
    assert len(await pw.sup("SELECT 1 FROM audit_logs WHERE organization_id=:o AND action='member.deactivate'", o=org)) == 1


@pytest.mark.asyncio
async def test_okta_put_and_pathless_patch_deactivate_too(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "okta")
    await _admin(pw, "oktaadmin", org)
    uid1, ident1 = await _linked_member(pw, api, org, raw, "oktamember1")
    uid2, ident2 = await _linked_member(pw, api, org, raw, "oktamember2")
    put = api.put(
        f"/scim/v2/Users/{ident1}", headers=_a(raw),
        json={"schemas": [USER], "userName": pw.email("oktamember1"), "name": {"givenName": "A", "familyName": "B"},
              "emails": [{"value": pw.email("oktamember1"), "primary": True, "type": "work"}], "active": False},
    )
    assert put.status_code == 200 and put.json()["active"] is False and put.json()["displayName"] == "A B"
    assert await _state(pw, org, uid1) == ("deactivated", "scim", 1)
    patch = api.patch(
        f"/scim/v2/Users/{ident2}", headers=_a(raw),
        json={"schemas": [PATCH], "Operations": [{"op": "replace", "value": {"active": False, "title": "x"}}]},
    )
    assert patch.status_code == 200 and await _state(pw, org, uid2) == ("deactivated", "scim", 1)


@pytest.mark.asyncio
async def test_delete_means_offboard_not_erase_and_a_repeat_is_a_404(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "del")
    await _admin(pw, "deladmin", org)
    uid, ident = await _linked_member(pw, api, org, raw, "delmember")
    projects = (await pw.sup("SELECT count(*) FROM projects WHERE organization_id=:o", o=org))[0][0]
    assert api.delete(f"/scim/v2/Users/{ident}", headers=_a(raw)).status_code == 204
    assert await _state(pw, org, uid) == ("deactivated", "scim", 1)
    assert (await pw.sup("SELECT count(*) FROM users WHERE id=:u", u=uid))[0][0] == 1  # the account is not deleted
    assert (await pw.sup("SELECT count(*) FROM projects WHERE organization_id=:o", o=org))[0][0] == projects  # nor any data
    assert api.get(f"/scim/v2/Users/{ident}", headers=_a(raw)).status_code == 404
    assert api.delete(f"/scim/v2/Users/{ident}", headers=_a(raw)).status_code == 404
    # deleting an identity that was never linked changes no member
    pending = _create(api, raw, "ghost@example.test").json()["id"]
    assert api.delete(f"/scim/v2/Users/{pending}", headers=_a(raw)).status_code == 204


@pytest.mark.asyncio
async def test_the_last_administrator_cannot_be_deactivated_by_scim(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "lastadm")
    admin = await _admin(pw, "lastadmadmin", org)
    subject = "sub-last-admin"
    await pw.sup("INSERT INTO external_identity_mappings (organization_id, user_id, idp_subject) VALUES (:o,:u,:s)", o=org, u=admin, s=subject)
    ident = _create(api, raw, pw.email("lastadmadmin"), externalId=subject).json()["id"]
    r = _entra_deactivate(api, raw, ident)
    assert r.status_code == 409 and "last administrator" in r.json()["detail"]
    assert await _state(pw, org, admin) is None
    assert (await _login_ok(api, pw, "lastadmadmin")) is True
    refused = await pw.sup("SELECT metadata->>'reason' FROM audit_logs WHERE organization_id=:o AND action='scim.deactivation_refused'", o=org)
    assert refused == [("last_admin",)]
    cfg = (await pw.sup("SELECT last_error_code FROM scim_configurations WHERE organization_id=:o", o=org))[0][0]
    assert cfg == "scim.last_admin"
    # a second administrator makes it allowed
    other_admin = await _admin(pw, "lastadmother", org)
    assert _entra_deactivate(api, raw, ident).status_code == 200
    assert await _state(pw, org, admin) == ("deactivated", "scim", 1)
    assert other_admin


async def _login_ok(api, pw, name):  # noqa: F811
    return api.post("/auth/login", json={"email": pw.email(name), "password": OLD_PW}).status_code == 200


# ================================================================ re-enabling: provenance and out-of-order requests


@pytest.mark.asyncio
async def test_a_reenable_is_deferred_while_settling_then_reactivates_and_old_tokens_stay_dead(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "settle", settle=300)
    await _admin(pw, "settleadmin", org)
    uid, ident = await _linked_member(pw, api, org, raw, "settlemember")
    old = _login(api, pw, "settlemember")
    _entra_deactivate(api, raw, ident)

    enable = {"schemas": [PATCH], "Operations": [{"op": "Replace", "path": "active", "value": "True"}]}
    late = api.patch(f"/scim/v2/Users/{ident}", headers=_a(raw), json=enable)  # an out-of-order "true" right after the "false"
    assert late.status_code == 409 and "deferred" in late.json()["detail"]
    assert await _state(pw, org, uid) == ("deactivated", "scim", 1)  # the newer deactivation was NOT undone

    await pw.sup("UPDATE scim_configurations SET reactivation_settle_seconds=0 WHERE organization_id=:o", o=org)
    ok = api.patch(f"/scim/v2/Users/{ident}", headers=_a(raw), json=enable)
    assert ok.status_code == 200 and ok.json()["active"] is True
    assert await _state(pw, org, uid) == ("active", "scim", 2)
    stale = api.get("/auth/me", headers=_h(old))
    assert stale.status_code == 403 and stale.json()["error_code"] == "auth.session_revoked"  # old token not revived
    assert api.post("/auth/refresh", json={"refresh_token": old["refresh_token"]}).status_code == 403
    assert (await pw.sup("SELECT count(*) FROM refresh_tokens WHERE organization_id=:o AND user_id=:u AND revoked_at IS NULL", o=org, u=uid))[0][0] == 0
    assert api.get("/auth/me", headers=_h(_login(api, pw, "settlemember"))).status_code == 200  # fresh authentication works
    # a duplicate re-enable is a no-op
    assert api.patch(f"/scim/v2/Users/{ident}", headers=_a(raw), json=enable).status_code == 200
    assert await _state(pw, org, uid) == ("active", "scim", 2)


@pytest.mark.asyncio
async def test_scim_never_overrides_an_administrators_deactivation(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "prov", settle=0)
    admin = await _admin(pw, "provadmin", org)
    uid, ident = await _linked_member(pw, api, org, raw, "provmember")
    admin_t = _login(api, pw, "provadmin")
    assert api.post(f"/organizations/{org}/members/{uid}/deactivate", headers=_h(admin_t), json={"reason": "policy"}).status_code == 200
    enable = {"schemas": [PATCH], "Operations": [{"op": "Replace", "path": "active", "value": "True"}]}
    r = api.patch(f"/scim/v2/Users/{ident}", headers=_a(raw), json=enable)
    assert r.status_code == 200 and r.json()["active"] is False  # EKIP's truth is reported
    assert await _state(pw, org, uid) == ("deactivated", "admin", 1)
    ignored = await pw.sup("SELECT metadata->>'reason' FROM audit_logs WHERE organization_id=:o AND action='scim.reactivation_ignored'", o=org)
    assert ignored == [("deactivated_by_administrator",)]
    assert admin


# ================================================================ the mass-deactivation guard


@pytest.mark.asyncio
async def test_the_mass_deactivation_guard_holds_the_excess_for_an_administrator(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "guard", guard_min=2, guard_percent=1)  # at most 2 deactivations per window
    admin = await _admin(pw, "guardadmin", org)
    members = [await _linked_member(pw, api, org, raw, f"guardm{i}") for i in range(4)]
    admin_t = _login(api, pw, "guardadmin")

    assert _entra_deactivate(api, raw, members[0][1]).status_code == 200
    assert _entra_deactivate(api, raw, members[1][1]).status_code == 200
    held = _entra_deactivate(api, raw, members[2][1])
    assert held.status_code == 409 and "held for administrator approval" in held.json()["detail"]
    assert await _state(pw, org, members[2][0]) is None  # NOT applied
    assert _entra_deactivate(api, raw, members[2][1]).status_code == 409  # the IdP retries: still held, still one record
    pending = api.get(f"/organizations/{org}/scim/pending", headers=_h(admin_t)).json()
    assert len(pending) == 1 and pending[0]["user_id"] == str(members[2][0]) and pending[0]["status"] == "pending"
    held_audit = await pw.sup("SELECT count(*) FROM audit_logs WHERE organization_id=:o AND action='scim.deactivation_held'", o=org)
    assert held_audit[0][0] >= 1
    assert (await pw.sup("SELECT last_error_code FROM scim_configurations WHERE organization_id=:o", o=org))[0][0] == "scim.deactivation_held"

    ok = api.post(f"/organizations/{org}/scim/pending/{pending[0]['id']}/approve", headers=_h(admin_t))
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    assert await _state(pw, org, members[2][0]) == ("deactivated", "scim", 1)
    assert api.post(f"/organizations/{org}/scim/pending/{pending[0]['id']}/approve", headers=_h(admin_t)).status_code == 409  # decided once

    # a different held request, this time rejected: the member stays active and the IdP's next attempt is held anew
    assert _entra_deactivate(api, raw, members[3][1]).status_code == 409
    second = api.get(f"/organizations/{org}/scim/pending", headers=_h(admin_t)).json()[0]
    rej = api.post(f"/organizations/{org}/scim/pending/{second['id']}/reject", headers=_h(admin_t))
    assert rej.status_code == 200 and rej.json()["status"] == "rejected"
    assert await _state(pw, org, members[3][0]) is None
    assert admin


@pytest.mark.asyncio
async def test_a_reenable_withdraws_a_held_deactivation(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "withdraw", guard_min=1, guard_percent=1, settle=0)
    admin = await _admin(pw, "withdrawadmin", org)
    first = await _linked_member(pw, api, org, raw, "withdraw1")
    second = await _linked_member(pw, api, org, raw, "withdraw2")
    assert _entra_deactivate(api, raw, first[1]).status_code == 200
    assert _entra_deactivate(api, raw, second[1]).status_code == 409  # held
    enable = {"schemas": [PATCH], "Operations": [{"op": "Replace", "path": "active", "value": "True"}]}
    assert api.patch(f"/scim/v2/Users/{second[1]}", headers=_a(raw), json=enable).status_code == 200
    assert api.get(f"/organizations/{org}/scim/pending", headers=_h(_login(api, pw, "withdrawadmin"))).json() == []
    assert admin


# ================================================================ rate limiting and health


@pytest.mark.asyncio
async def test_each_token_has_its_own_rate_limit(pw, api, monkeypatch) -> None:  # noqa: F811
    _enable(monkeypatch, scim_requests_per_minute=10)
    a, raw_a = await _scim_org(pw, "rla")
    b, raw_b = await _scim_org(pw, "rlb")
    codes = [api.get("/scim/v2/Users", headers=_a(raw_a)).status_code for _ in range(12)]
    assert codes[:10] == [200] * 10 and codes[10] == 429
    limited = api.get("/scim/v2/Users", headers=_a(raw_a))
    assert limited.json()["status"] == "429" and limited.headers["retry-after"]
    assert api.get("/scim/v2/Users", headers=_a(raw_b)).status_code == 200  # another organization's token is unaffected


@pytest.mark.asyncio
async def test_health_fields_track_the_last_request_success_and_error(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "health")
    before = (await pw.sup("SELECT last_request_at, last_success_at, last_error_code FROM scim_configurations WHERE organization_id=:o", o=org))[0]
    assert before == (None, None, None)
    assert api.get("/scim/v2/Users", headers=_a(raw)).status_code == 200
    ok = (await pw.sup("SELECT last_request_at IS NOT NULL, last_success_at IS NOT NULL, last_error_code FROM scim_configurations WHERE organization_id=:o", o=org))[0]
    assert ok == (True, True, None)
    assert api.get(f"/scim/v2/Users/{uuid.uuid4()}", headers=_a(raw)).status_code == 404
    err = (await pw.sup("SELECT last_error_code, last_error_at IS NOT NULL FROM scim_configurations WHERE organization_id=:o", o=org))[0]
    assert err == ("scim.not_found", True)
    assert (await pw.sup("SELECT last_used_at IS NOT NULL FROM scim_tokens WHERE organization_id=:o", o=org))[0][0] is True


# ================================================================ tenant-admin API: tokens, configuration, linking


@pytest.mark.asyncio
async def test_token_lifecycle_through_the_admin_api(pw, api, monkeypatch) -> None:  # noqa: F811
    org = await _org(pw, "tok")
    await _admin(pw, "tokadmin", org)
    t = _login(api, pw, "tokadmin")

    overview = api.get(f"/organizations/{org}/scim", headers=_h(t)).json()
    assert overview["enabled"] is False and overview["platform_enabled"] is True and overview["active_tokens"] == 0
    assert overview["endpoint_url"].endswith("/scim/v2")
    assert api.put(f"/organizations/{org}/scim", headers=_h(t), json={"enabled": True}).json()["enabled"] is True

    created = api.post(f"/organizations/{org}/scim/tokens", headers=_h(t), json={"name": "Entra", "ttl_days": 30})
    assert created.status_code == 201
    raw = created.json()["raw_token"]
    info = created.json()["token"]
    assert raw.startswith("scim_") and info["prefix"] == raw[:12] and info["status"] == "active"
    # only the hash is stored, the raw value appears in no column, and the listing never returns it
    row = (await pw.sup("SELECT token_hash, token_prefix, name FROM scim_tokens WHERE id=:i", i=uuid.UUID(info["id"])))[0]
    assert row[0] == tokens.hash_token(raw) and raw not in row and raw != row[0]
    assert raw not in str(await pw.sup("SELECT to_jsonb(t) FROM scim_tokens t WHERE id=:i", i=uuid.UUID(info["id"])))
    listing = api.get(f"/organizations/{org}/scim/tokens", headers=_h(t))
    assert raw not in listing.text and listing.json()[0]["prefix"] == raw[:12]
    assert api.get("/scim/v2/Users", headers=_a(raw)).status_code == 200

    rotated = api.post(f"/organizations/{org}/scim/tokens/{info['id']}/rotate", headers=_h(t), json={})
    assert rotated.status_code == 201
    new_raw = rotated.json()["raw_token"]
    assert new_raw != raw
    assert api.get("/scim/v2/Users", headers=_a(raw)).status_code == 200  # overlap: both work during the grace period
    assert api.get("/scim/v2/Users", headers=_a(new_raw)).status_code == 200
    old_expiry = (await pw.sup("SELECT expires_at FROM scim_tokens WHERE id=:i", i=uuid.UUID(info["id"])))[0][0]
    assert old_expiry <= datetime.now(UTC) + timedelta(hours=24, minutes=1)  # the old one now dies within a day

    revoked = api.post(f"/organizations/{org}/scim/tokens/{info['id']}/revoke", headers=_h(t))
    assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
    assert api.get("/scim/v2/Users", headers=_a(raw)).status_code == 401
    assert api.get("/scim/v2/Users", headers=_a(new_raw)).status_code == 200
    assert api.post(f"/organizations/{org}/scim/tokens/{info['id']}/rotate", headers=_h(t), json={}).status_code == 409  # not active
    # expiry is enforced
    await pw.sup("UPDATE scim_tokens SET expires_at = now() - interval '1 second' WHERE token_hash=:h", h=tokens.hash_token(new_raw))
    assert api.get("/scim/v2/Users", headers=_a(new_raw)).status_code == 401
    actions = {r[0] for r in await pw.sup("SELECT action FROM audit_logs WHERE organization_id=:o AND action LIKE 'scim.%'", o=org)}
    assert {"scim.token_created", "scim.token_rotated", "scim.token_revoked", "scim.config_updated"} <= actions
    assert api.get(f"/organizations/{org}/scim/tokens/{uuid.uuid4()}", headers=_h(t)).status_code in (404, 405)
    assert api.post(f"/organizations/{org}/scim/tokens/{uuid.uuid4()}/revoke", headers=_h(t)).status_code == 404


@pytest.mark.asyncio
async def test_token_limits_and_validation(pw, api, monkeypatch) -> None:  # noqa: F811
    _enable(monkeypatch, scim_max_active_tokens=2, scim_token_max_ttl_days=10)
    org = await _org(pw, "lim")
    await _admin(pw, "limadmin", org)
    t = _login(api, pw, "limadmin")
    url = f"/organizations/{org}/scim/tokens"
    assert api.post(url, headers=_h(t), json={"name": "a", "ttl_days": 11}).status_code == 422 or api.post(url, headers=_h(t), json={"name": "a", "ttl_days": 11}).status_code == 400
    assert api.post(url, headers=_h(t), json={"name": "", "ttl_days": 5}).status_code == 422
    assert api.post(url, headers=_h(t), json={"name": "one", "ttl_days": 5}).status_code == 201
    assert api.post(url, headers=_h(t), json={"name": "two", "ttl_days": 5}).status_code == 201
    third = api.post(url, headers=_h(t), json={"name": "three", "ttl_days": 5})
    assert third.status_code == 409 and third.json()["error_code"] == "scim.too_many_tokens"


@pytest.mark.asyncio
async def test_only_administrators_of_the_organization_may_manage_provisioning(pw, api) -> None:  # noqa: F811
    a, b = await _org(pw, "pa"), await _org(pw, "pb")
    await _admin(pw, "padmina", a)
    await _admin(pw, "padminb", b)
    await _user(pw, "pplain", a)
    plain, admin_b = _login(api, pw, "pplain"), _login(api, pw, "padminb")
    calls = (
        ("get", f"/organizations/{a}/scim", None),
        ("put", f"/organizations/{a}/scim", {"enabled": True}),
        ("get", f"/organizations/{a}/scim/tokens", None),
        ("post", f"/organizations/{a}/scim/tokens", {"name": "x"}),
        ("get", f"/organizations/{a}/scim/identities", None),
        ("get", f"/organizations/{a}/scim/pending", None),
        ("post", f"/organizations/{a}/scim/identities/{uuid.uuid4()}/link", {"user_id": str(uuid.uuid4())}),
    )
    for method, path, body in calls:
        kwargs = {"json": body} if body is not None else {}
        assert getattr(api, method)(path, headers=_h(plain), **kwargs).status_code == 403, path  # no tenancy:manage
        r = getattr(api, method)(path, headers=_h(admin_b), **kwargs)  # an administrator of ANOTHER organization
        assert r.status_code == 403 and r.json()["error_code"] == "users.cross_organization_denied", path
        assert getattr(api, method)(path, **kwargs).status_code == 403, path  # nobody


@pytest.mark.asyncio
async def test_an_administrator_links_a_pending_identity_by_hand_and_an_inactive_one_is_offboarded(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "link", settle=0)
    admin = await _admin(pw, "linkadmin", org)
    member = await _user(pw, "linkmember", org)
    t = _login(api, pw, "linkadmin")
    ident = _create(api, raw, pw.email("linkmember")).json()["id"]  # same email: only a HINT
    page = api.get(f"/organizations/{org}/scim/identities", headers=_h(t), params={"status": "pending"}).json()
    entry = next(i for i in page["items"] if i["id"] == ident)
    assert entry["linked_user_id"] is None and [c["user_id"] for c in entry["candidates"]] == [str(member)]

    assert api.post(f"/organizations/{org}/scim/identities/{ident}/link", headers=_h(t), json={"user_id": str(uuid.uuid4())}).status_code == 409  # not a member
    linked = api.post(f"/organizations/{org}/scim/identities/{ident}/link", headers=_h(t), json={"user_id": str(member)})
    assert linked.status_code == 200 and linked.json()["linked_via"] == "admin" and linked.json()["linked_user_id"] == str(member)
    assert api.post(f"/organizations/{org}/scim/identities/{ident}/link", headers=_h(t), json={"user_id": str(member)}).status_code == 409
    other = _create(api, raw, "second@example.test").json()["id"]
    assert api.post(f"/organizations/{org}/scim/identities/{other}/link", headers=_h(t), json={"user_id": str(member)}).status_code == 409  # one identity per member
    audit = await pw.sup("SELECT metadata->>'via', actor FROM audit_logs WHERE organization_id=:o AND action='scim.identity_linked'", o=org)
    assert audit == [("admin", f"user:{admin}")]

    # an identity the IdP already reported inactive is offboarded the moment it is linked
    ghost_user = await _user(pw, "linkghost", org)
    ghost = _create(api, raw, "ghost@example.test", active=False).json()["id"]
    assert api.post(f"/organizations/{org}/scim/identities/{ghost}/link", headers=_h(t), json={"user_id": str(ghost_user)}).status_code == 200
    assert await _state(pw, org, ghost_user) == ("deactivated", "scim", 1)
    # an unknown identity id is a 404, and another organization's identity is invisible
    assert api.post(f"/organizations/{org}/scim/identities/{uuid.uuid4()}/link", headers=_h(t), json={"user_id": str(member)}).status_code == 404
    other_org, other_raw = await _scim_org(pw, "linkother")
    foreign = _create(api, other_raw, "foreign@example.test").json()["id"]
    assert api.post(f"/organizations/{org}/scim/identities/{foreign}/link", headers=_h(t), json={"user_id": str(member)}).status_code == 404


@pytest.mark.asyncio
async def test_scim_events_are_visible_in_the_audit_log_to_audit_readers_only(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "aud")
    await _admin(pw, "audadmin", org)
    await _user(pw, "audplain", org)
    uid, ident = await _linked_member(pw, api, org, raw, "audmember")
    _entra_deactivate(api, raw, ident)
    t = _login(api, pw, "audadmin")
    rows = api.get(f"/organizations/{org}/audit", headers=_h(t), params={"limit": 100}).json()
    actions = {r["action"] for r in rows}
    assert {"scim.user_created", "scim.identity_linked", "member.deactivate"} <= actions
    deact = next(r for r in rows if r["action"] == "member.deactivate")
    assert deact["actor"].startswith("agent:scim:") and deact["event_metadata"]["source"] == "scim"
    assert raw not in str(rows)  # no credential ever reaches the audit trail
    assert api.get(f"/organizations/{org}/audit", headers=_h(_login(api, pw, "audplain"))).status_code == 403
    assert uid
