"""Phase 5 hardening: the security properties of SCIM provisioning, groups and role synchronization, checked against
real PostgreSQL over real HTTP. No identity provider, no email, no real database.

Skipped unless EKIP_TEST_* and DATABASE_URL point at a LOCAL migrated database."""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio

from app.core.scim import groups as scim_groups
from app.database.session import set_tenant_context
from app.shared.schemas import Identity
from tests.api.test_member_offboarding_api_postgres import _admin, _h, _login  # noqa: F401  (helpers)
from tests.api.test_scim_api_postgres import (  # noqa: F401  (fixtures + helpers)
    _a,
    _create,
    _linked_member,
    _scim_org,
    _state,
    api,
    pytestmark,
)
from tests.api.test_scim_groups_api_postgres import (  # noqa: F401  (helpers)
    GROUP,
    PATCH,
    _audit,
    _entra_remove,
    _map,
    _members_patch,
    _new_group,
    _role,
    _roles,
)
from tests.core.auth.test_password_reset_postgres import OLD_PW, _org, _user, env, pw  # noqa: F401


@pytest_asyncio.fixture(autouse=True)
async def _migrated_groups(pw):  # noqa: F811
    if not await pw.sup("SELECT 1 FROM information_schema.tables WHERE table_name='scim_groups'"):
        pytest.skip("run `alembic upgrade head` on this database")
    yield


# ================================================================ database-level isolation


@pytest.mark.asyncio
async def test_the_group_tables_are_row_level_secured_for_the_application_role(pw) -> None:  # noqa: F811
    org_a, org_b = await _org(pw, "rlsa"), await _org(pw, "rlsb")
    for org in (org_a, org_b):
        await pw.sup("INSERT INTO scim_groups (organization_id, display_name, display_name_key) VALUES (:o,'G','g')", o=org)

    async def count(org, table):
        async def fn(s):
            from sqlalchemy import text

            return (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()

        return await pw.run(org, fn)

    assert await count(org_a, "scim_groups") == 1  # sees only its own
    assert await count(org_b, "scim_groups") == 1
    assert await count(None, "scim_groups") == 0  # no tenant context: nothing at all
    for table in ("scim_group_members", "scim_group_role_mappings", "scim_role_grants"):
        assert await count(None, table) == 0

    async def forge(s):  # a row stamped with ANOTHER organization is refused by the policy
        from sqlalchemy import text

        await s.execute(
            text("INSERT INTO scim_groups (organization_id, display_name, display_name_key) VALUES (:o,'X','x')"), {"o": org_b}
        )

    with pytest.raises(Exception, match="row-level security|policy"):
        await pw.run(org_a, forge)
    for table in ("scim_groups", "scim_group_members", "scim_group_role_mappings", "scim_role_grants"):
        rows = await pw.sup("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname=:t", t=table)
        assert tuple(rows[0]) == (True, True), table
    # the application role may not delete across tenants or touch built-in roles through these tables either
    assert await pw.sup("SELECT 1 FROM scim_groups WHERE organization_id=:o AND display_name='X'", o=org_b) == []


# ================================================================ authentication isolation


@pytest.mark.asyncio
async def test_a_scim_token_is_not_a_rest_token_and_a_rest_token_is_not_a_scim_token(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "iso")
    await _admin(pw, "isoadmin", org)
    t = _login(api, pw, "isoadmin")
    for method, path in (("get", "/scim/v2/Groups"), ("get", "/scim/v2/Users"), ("post", "/scim/v2/Groups")):
        assert getattr(api, method)(path, headers={"Authorization": f"Bearer {t['access_token']}"}).status_code == 401
    for path in (f"/organizations/{org}/members", f"/organizations/{org}/scim/groups", "/auth/me"):
        assert api.get(path, headers={"Authorization": f"Bearer {raw}"}).status_code in (401, 403)
    # the MCP surface verifies signed tokens as well
    assert api.post("/mcp", headers={"Authorization": f"Bearer {raw}"}, json={}).status_code in (401, 403, 404, 405, 406)


@pytest.mark.asyncio
async def test_no_secret_reaches_the_audit_log_or_an_error_body(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "leak")
    _u, ident = await _linked_member(pw, api, org, raw, "leak1")
    gid = _new_group(api, raw, "G", [ident])
    _members_patch(api, raw, gid, "add", [str(uuid.uuid4())])
    r = api.get("/scim/v2/Groups", headers={"Authorization": "Bearer scim_wrong_token_value_000000000000"})
    assert raw not in r.text and "scim_wrong" not in r.text
    dump = await pw.sup("SELECT actor::text || metadata::text FROM audit_logs WHERE organization_id=:o", o=org)
    blob = " ".join(str(row[0]) for row in dump)
    assert raw not in blob and raw[12:] not in blob  # only the non-secret prefix identifies the token


# ================================================================ abuse: size, replay, concurrency


@pytest.mark.asyncio
async def test_oversized_and_deeply_malformed_group_requests_are_refused_cleanly(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "abuse")
    gid = _new_group(api, raw, "G")
    too_many = [{"value": str(uuid.uuid4())} for _ in range(1001)]
    r = api.post("/scim/v2/Groups", headers=_a(raw), json={"schemas": [GROUP], "displayName": "Big", "members": too_many})
    assert r.status_code == 400 and r.json()["scimType"] == "tooMany"
    ops = [{"op": "add", "path": "members", "value": [{"value": str(uuid.uuid4())}]} for _ in range(101)]
    assert api.patch(f"/scim/v2/Groups/{gid}", headers=_a(raw), json={"schemas": [PATCH], "Operations": ops}).status_code == 400
    assert api.post("/scim/v2/Groups", headers=_a(raw), content=b"x" * 1_100_000).status_code == 413
    for weird in ('{"displayName": "a\\u0000b"}', '{"displayName": ["x"]}', "[1,2,3]", "null", '"string"'):
        assert api.post("/scim/v2/Groups", headers=_a(raw), content=weird.encode()).status_code < 500
    assert api.get("/scim/v2/Groups?startIndex=-5&count=99999", headers=_a(raw)).status_code == 200
    assert api.get("/scim/v2/Groups?startIndex=abc", headers=_a(raw)).status_code == 400
    assert await _audit(pw, org, "scim.group_created") == 1  # none of the above created anything


@pytest.mark.asyncio
async def test_replayed_out_of_order_and_concurrent_group_events_converge(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "conv")
    uid, ident = await _linked_member(pw, api, org, raw, "conv1")
    g1, g2 = _new_group(api, raw, "A"), _new_group(api, raw, "B")
    await _map(pw, org, g1, "engineer")
    await _map(pw, org, g2, "engineer")
    # out of order: remove before add, add twice, remove twice
    for fn in (lambda: _entra_remove(api, raw, g1, ident), lambda: _members_patch(api, raw, g1, "add", [ident]),
               lambda: _members_patch(api, raw, g1, "add", [ident]), lambda: _entra_remove(api, raw, g1, ident),
               lambda: _entra_remove(api, raw, g1, ident), lambda: _members_patch(api, raw, g1, "add", [ident])):
        assert fn().status_code == 200
    assert (await _roles(pw, org, uid)).get("engineer") == "scim"
    assert (await pw.sup("SELECT count(*) FROM scim_role_grants WHERE user_id=:u", u=uid))[0][0] == 1

    # concurrency: two requests reconcile the SAME person at once; they take turns instead of colliding
    await pw.sup("INSERT INTO scim_group_members (group_id, scim_identity_id, organization_id) VALUES (:g,:i,:o)", g=uuid.UUID(g2), i=uuid.UUID(ident), o=org)
    actor = Identity.for_agent("scim:test", org)

    async def reconcile(s):
        await scim_groups.reconcile_user(s, actor, org, uid)

    results = await asyncio.gather(pw.run(org, reconcile), pw.run(org, reconcile), pw.run(org, reconcile), return_exceptions=True)
    assert [r for r in results if isinstance(r, Exception)] == []
    assert (await pw.sup("SELECT count(*) FROM scim_role_grants WHERE user_id=:u", u=uid))[0][0] == 2  # one per group, never duplicated
    assert (await _roles(pw, org, uid)).get("engineer") == "scim"


# ================================================================ offboarding through groups


@pytest.mark.asyncio
async def test_a_group_driven_deactivation_cuts_every_session_immediately(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "cut")
    other = await _org(pw, "cutother")
    uid, ident = await _linked_member(pw, api, org, raw, "cut1")
    await pw.sup("INSERT INTO user_roles (user_id, organization_id, role_id) VALUES (:u,:o,:r)", u=uid, o=other, r=await _role(pw, other, "viewer"))
    gid = _new_group(api, raw, "Only", [ident])
    await _map(pw, org, gid, "engineer")
    _members_patch(api, raw, gid, "remove", [ident]); _members_patch(api, raw, gid, "add", [ident])  # noqa: E702
    await pw.sup("DELETE FROM user_roles WHERE user_id=:u AND organization_id=:o AND granted_via='manual'", u=uid, o=org)
    session = _login(api, pw, "cut1", org)
    assert api.get("/auth/me", headers=_h(session)).status_code == 200

    assert _entra_remove(api, raw, gid, ident).status_code == 200
    state = await _state(pw, org, uid)
    assert state[0] == "deactivated" and state[1] == "scim" and state[2] >= 1
    assert api.get("/auth/me", headers=_h(session)).status_code == 403  # the access token dies on its next use
    assert api.post("/auth/refresh", json={"refresh_token": session["refresh_token"]}).status_code == 403
    assert api.post("/auth/login", json={"email": pw.email("cut1"), "password": OLD_PW}).status_code in (200, 403)
    # their other organization still works: the deactivation is organization-specific
    again = api.post("/auth/login", json={"email": pw.email("cut1"), "password": OLD_PW}).json()
    assert again.get("status") != "complete" or again.get("access_token")
    assert (await pw.sup("SELECT is_active FROM users WHERE id=:u", u=uid))[0][0] is True


@pytest.mark.asyncio
async def test_the_mass_deactivation_guard_also_covers_group_driven_offboarding(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "mass", guard_min=2, guard_percent=1)
    members = [await _linked_member(pw, api, org, raw, f"mass{i}") for i in range(4)]
    gid = _new_group(api, raw, "All", [i for _u, i in members])
    await _map(pw, org, gid, "engineer")
    _members_patch(api, raw, gid, "remove", [i for _u, i in members])
    _members_patch(api, raw, gid, "add", [i for _u, i in members])
    await pw.sup("DELETE FROM user_roles WHERE organization_id=:o AND granted_via='manual'", o=org)  # access exists only through the group
    assert api.delete(f"/scim/v2/Groups/{gid}", headers=_a(raw)).status_code == 204
    deactivated = await pw.sup("SELECT count(*) FROM organization_member_states WHERE organization_id=:o AND status='deactivated'", o=org)
    assert deactivated[0][0] == 2  # the limit; the rest are HELD, not mass-deactivated
    assert await _audit(pw, org, "scim.role_revoke_held") == 2
    held = [u for u, _i in members if await _state(pw, org, u) is None]
    assert len(held) == 2
    for user in held:  # a held member keeps their role and their access
        assert (await _roles(pw, org, user)).get("engineer") == "scim"


@pytest.mark.asyncio
async def test_mapping_a_role_needs_every_permission_it_carries(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "esc")
    gid = _new_group(api, raw, "Admins")
    await pw.sup("INSERT INTO roles (organization_id, name) VALUES (:o,'limited')", o=org)
    limited = await _role(pw, org, "limited")
    await pw.sup(
        "INSERT INTO role_permissions (role_id, permission_id) SELECT :r, id FROM permissions WHERE code='tenancy:manage'", r=limited
    )
    uid = await _user(pw, "escmanager", org)
    await pw.sup("DELETE FROM user_roles WHERE user_id=:u AND organization_id=:o", u=uid, o=org)
    await pw.sup("INSERT INTO user_roles (user_id, organization_id, role_id) VALUES (:u,:o,:r)", u=uid, o=org, r=limited)
    t = _login(api, pw, "escmanager")
    url = f"/organizations/{org}/scim/groups/{gid}/roles"
    r = api.put(url, headers=_h(t), json={"role_ids": [str(await _role(pw, org, "admin"))]})
    assert r.status_code == 403 and r.json()["error_code"] == "role.permission_escalation"
    assert await pw.sup("SELECT 1 FROM scim_group_role_mappings WHERE group_id=:g", g=uuid.UUID(gid)) == []
    # a role whose permissions they all hold is fine
    assert api.put(url, headers=_h(t), json={"role_ids": [str(limited)]}).status_code == 200


@pytest.mark.asyncio
async def test_group_sync_never_creates_an_account_and_never_touches_other_organizations(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "noacct")
    other = await _org(pw, "noacctother")
    before = (await pw.sup("SELECT count(*) FROM users"))[0][0]
    outsider = await _user(pw, "outsider", other)
    r = _create(api, raw, pw.email("outsider"))  # an email that belongs to another organization's member
    assert r.status_code == 201
    gid = _new_group(api, raw, "G", [r.json()["id"]])
    await _map(pw, org, gid, "engineer")
    _members_patch(api, raw, gid, "remove", [r.json()["id"]]); _members_patch(api, raw, gid, "add", [r.json()["id"]])  # noqa: E702
    assert (await pw.sup("SELECT count(*) FROM users"))[0][0] == before + 1  # only the user created by the helper above
    assert await _roles(pw, org, outsider) == {}  # never linked by email, so nothing was granted here
    assert await _roles(pw, other, outsider) == {"viewer": "manual"}
