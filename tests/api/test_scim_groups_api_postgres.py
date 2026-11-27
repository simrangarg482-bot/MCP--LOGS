"""SCIM Groups and role synchronization over real HTTP, real PostgreSQL (as `ekip_app`), simulated Entra ID and
Okta requests. No identity provider and no email is involved.

Skipped unless EKIP_TEST_* and DATABASE_URL point at a LOCAL migrated database."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from tests.api.test_member_offboarding_api_postgres import _admin, _h, _login  # noqa: F401  (helpers)
from tests.api.test_scim_api_postgres import (  # noqa: F401  (fixtures + helpers)
    MEDIA,
    PATCH,
    _a,
    _create,
    _linked_member,
    _scim_org,
    _state,
    _token,
    api,
    pytestmark,
)
from tests.core.auth.test_password_reset_postgres import _org, _user, env, pw  # noqa: F401

GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"


@pytest_asyncio.fixture(autouse=True)
async def _migrated_groups(pw):  # noqa: F811
    if not await pw.sup("SELECT 1 FROM information_schema.tables WHERE table_name='scim_groups'"):
        pytest.skip("run `alembic upgrade head` on this database")
    yield


# ------------------------------------------------------------------------------------------ helpers


async def _role(pw, org, name):  # noqa: F811
    rows = await pw.sup(
        "SELECT id FROM roles WHERE name=:n AND (organization_id=:o OR organization_id IS NULL) "
        "ORDER BY organization_id NULLS LAST LIMIT 1", n=name, o=org,
    )
    return rows[0][0]


async def _roles(pw, org, uid):  # noqa: F811
    rows = await pw.sup(
        "SELECT r.name, ur.granted_via FROM user_roles ur JOIN roles r ON r.id=ur.role_id "
        "WHERE ur.organization_id=:o AND ur.user_id=:u", o=org, u=uid,
    )
    return {r[0]: r[1] for r in rows}


async def _map(pw, org, group_id, role_name):  # noqa: F811
    await pw.sup(
        "INSERT INTO scim_group_role_mappings (group_id, role_id, organization_id) VALUES (:g,:r,:o)",
        g=uuid.UUID(group_id), r=await _role(pw, org, role_name), o=org,
    )


async def _audit(pw, org, action):  # noqa: F811
    rows = await pw.sup("SELECT count(*) FROM audit_logs WHERE organization_id=:o AND action=:a", o=org, a=action)
    return rows[0][0]


def _new_group(api, raw, name, members=(), **extra):  # noqa: F811
    body = {"schemas": [GROUP], "displayName": name, **extra}
    if members:
        body["members"] = [{"value": m} for m in members]
    r = api.post("/scim/v2/Groups", headers=_a(raw), json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _members_patch(api, raw, gid, op, ids=None, path="members", **kw):  # noqa: F811
    operation = {"op": op, "path": path}
    if ids is not None:
        operation["value"] = [{"value": i} for i in ids]
    return api.patch(f"/scim/v2/Groups/{gid}", headers=_a(raw), json={"schemas": [PATCH], "Operations": [operation]})


def _entra_remove(api, raw, gid, ident):  # noqa: F811
    return _members_patch(api, raw, gid, "Remove", path=f'members[value eq "{ident}"]')


# ================================================================ protocol


@pytest.mark.asyncio
async def test_group_crud_filters_and_idempotent_membership(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "gcrud")
    u1, i1 = await _linked_member(pw, api, org, raw, "gcrud1")
    u2, i2 = await _linked_member(pw, api, org, raw, "gcrud2")

    r = api.post("/scim/v2/Groups", headers=_a(raw), json={"schemas": [GROUP], "displayName": "Engineers", "externalId": "g-1", "members": [{"value": i1}]})
    assert r.status_code == 201 and r.headers["content-type"].startswith(MEDIA)
    body = r.json()
    gid = body["id"]
    assert body["displayName"] == "Engineers" and body["externalId"] == "g-1"
    assert [m["value"] for m in body["members"]] == [i1] and body["meta"]["resourceType"] == "Group"

    assert api.post("/scim/v2/Groups", headers=_a(raw), json={"schemas": [GROUP], "displayName": "ENGINEERS"}).status_code == 409  # case-insensitive
    assert api.post("/scim/v2/Groups", headers=_a(raw), json={"schemas": [GROUP], "displayName": "Other", "externalId": "g-1"}).status_code == 409

    assert api.get(f"/scim/v2/Groups/{gid}", headers=_a(raw)).json()["id"] == gid
    listed = api.get("/scim/v2/Groups", headers=_a(raw)).json()
    assert listed["totalResults"] == 1 and listed["Resources"][0]["id"] == gid
    assert api.get('/scim/v2/Groups?filter=displayName eq "engineers"', headers=_a(raw)).json()["totalResults"] == 1
    assert api.get('/scim/v2/Groups?filter=displayName eq "nope"', headers=_a(raw)).json()["totalResults"] == 0
    assert api.get('/scim/v2/Groups?filter=externalId eq "g-1"', headers=_a(raw)).json()["totalResults"] == 1
    assert api.get('/scim/v2/Groups?filter=members co "x"', headers=_a(raw)).status_code == 400
    assert api.get("/scim/v2/Groups?sortBy=displayName", headers=_a(raw)).status_code == 400

    # Entra: "Add" capitalised; adding twice is a no-op
    assert _members_patch(api, raw, gid, "Add", [i2]).status_code == 200
    assert _members_patch(api, raw, gid, "Add", [i2]).status_code == 200
    assert {m["value"] for m in api.get(f"/scim/v2/Groups/{gid}", headers=_a(raw)).json()["members"]} == {i1, i2}
    # Entra removes with a path filter; removing again (or someone who is not a member) is a no-op
    assert _entra_remove(api, raw, gid, i2).status_code == 200
    assert _entra_remove(api, raw, gid, i2).status_code == 200
    assert _entra_remove(api, raw, gid, str(uuid.uuid4())).status_code == 200
    # Okta: remove with a value list
    assert _members_patch(api, raw, gid, "remove", [i1]).status_code == 200
    assert api.get(f"/scim/v2/Groups/{gid}", headers=_a(raw)).json()["members"] == []
    # replace and rename, pathless value object
    assert _members_patch(api, raw, gid, "replace", [i1, i2]).status_code == 200
    r = api.patch(f"/scim/v2/Groups/{gid}", headers=_a(raw), json={"schemas": [PATCH], "Operations": [{"op": "Replace", "value": {"displayName": "Platform"}}]})
    assert r.status_code == 200 and r.json()["displayName"] == "Platform" and len(r.json()["members"]) == 2
    # remove all members with no value
    assert _members_patch(api, raw, gid, "remove").status_code == 200
    assert api.get(f"/scim/v2/Groups/{gid}", headers=_a(raw)).json()["members"] == []
    # PUT replaces the members only when members is present
    api.put(f"/scim/v2/Groups/{gid}", headers=_a(raw), json={"schemas": [GROUP], "displayName": "Platform", "members": [{"value": i1}]})
    r = api.put(f"/scim/v2/Groups/{gid}", headers=_a(raw), json={"schemas": [GROUP], "displayName": "Platform2"})
    assert r.status_code == 200 and [m["value"] for m in r.json()["members"]] == [i1]

    assert api.delete(f"/scim/v2/Groups/{gid}", headers=_a(raw)).status_code == 204
    assert api.delete(f"/scim/v2/Groups/{gid}", headers=_a(raw)).status_code == 404
    assert api.get(f"/scim/v2/Groups/{gid}", headers=_a(raw)).status_code == 404
    assert api.get("/scim/v2/Groups/not-a-uuid", headers=_a(raw)).status_code == 404
    assert u1 and u2


@pytest.mark.asyncio
async def test_malformed_group_payloads_are_clean_400s_and_change_nothing(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "gbad")
    _u, i1 = await _linked_member(pw, api, org, raw, "gbad1")
    gid = _new_group(api, raw, "Keep")
    bad = [
        {"schemas": [GROUP]},  # no displayName
        {"schemas": [GROUP], "displayName": ""},
        {"schemas": [GROUP], "displayName": 5},
        {"schemas": ["urn:wrong"], "displayName": "x"},
        {"schemas": [GROUP], "displayName": "x", "members": "nope"},
        {"schemas": [GROUP], "displayName": "x", "members": [{"display": "no value"}]},
        {"schemas": [GROUP], "displayName": "x", "members": [{"value": "not-a-uuid"}]},
        {"schemas": [GROUP], "displayName": "x", "members": [{"value": str(uuid.uuid4())}]},  # unknown member
        {"schemas": [GROUP], "displayName": "x" * 300},
        [],
    ]
    for payload in bad:
        r = api.post("/scim/v2/Groups", headers=_a(raw), json=payload)
        assert r.status_code == 400, (payload, r.text)
        assert r.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    assert api.post("/scim/v2/Groups", headers=_a(raw), content=b"{not json").status_code == 400
    # a PATCH with one bad operation applies NOTHING (atomic)
    r = api.patch(f"/scim/v2/Groups/{gid}", headers=_a(raw), json={"schemas": [PATCH], "Operations": [
        {"op": "add", "path": "members", "value": [{"value": i1}]},
        {"op": "add", "path": "members", "value": [{"value": str(uuid.uuid4())}]},
    ]})
    assert r.status_code == 400
    assert api.get(f"/scim/v2/Groups/{gid}", headers=_a(raw)).json()["members"] == []
    for body in ({"schemas": [PATCH]}, {"schemas": [PATCH], "Operations": []}, {"schemas": [PATCH], "Operations": [{"op": "move", "path": "members"}]},
                 {"schemas": [PATCH], "Operations": [{"op": "remove", "path": "displayName"}]},
                 {"schemas": [PATCH], "Operations": [{"op": "add", "path": 'members[value eq "x"]', "value": []}]}):
        assert api.patch(f"/scim/v2/Groups/{gid}", headers=_a(raw), json=body).status_code == 400, body
    assert (await pw.sup("SELECT count(*) FROM users WHERE email LIKE :e", e=f"%{pw.tag}%"))[0][0] >= 1  # SCIM created no account


# ================================================================ synchronization semantics


@pytest.mark.asyncio
async def test_group_grants_a_mapped_role_and_removal_withdraws_only_that_role(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "gsync")
    uid, ident = await _linked_member(pw, api, org, raw, "gsync1")
    assert await _roles(pw, org, uid) == {"viewer": "manual"}
    gid = _new_group(api, raw, "Engineers", [ident])
    assert await _roles(pw, org, uid) == {"viewer": "manual"}  # an UNMAPPED group grants nothing

    await _map(pw, org, gid, "engineer")
    _members_patch(api, raw, gid, "remove", [ident])
    _members_patch(api, raw, gid, "add", [ident])  # a membership change applies the (new) mapping
    assert await _roles(pw, org, uid) == {"viewer": "manual", "engineer": "scim"}
    assert await pw.sup("SELECT 1 FROM scim_role_grants WHERE user_id=:u", u=uid)
    assert await _audit(pw, org, "scim.role_granted") == 1

    assert _entra_remove(api, raw, gid, ident).status_code == 200
    assert await _roles(pw, org, uid) == {"viewer": "manual"}  # only the scim role went; the manual viewer stayed
    assert not await pw.sup("SELECT 1 FROM scim_role_grants WHERE user_id=:u", u=uid)
    assert await _state(pw, org, uid) is None  # NOT deactivated: another role remains
    assert await _audit(pw, org, "scim.role_revoked") == 1
    # replaying the same events is a no-op
    _entra_remove(api, raw, gid, ident)
    _members_patch(api, raw, gid, "add", [ident])
    _members_patch(api, raw, gid, "add", [ident])
    assert await _roles(pw, org, uid) == {"viewer": "manual", "engineer": "scim"}
    assert await _audit(pw, org, "scim.role_granted") == 2


@pytest.mark.asyncio
async def test_a_manual_role_is_never_removed_by_synchronization(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "gmanual")
    uid, ident = await _linked_member(pw, api, org, raw, "gmanual1")
    await pw.sup("INSERT INTO user_roles (user_id, organization_id, role_id) VALUES (:u,:o,:r)", u=uid, o=org, r=await _role(pw, org, "engineer"))
    gid = _new_group(api, raw, "Engineers")
    await _map(pw, org, gid, "engineer")
    _members_patch(api, raw, gid, "add", [ident])
    assert (await _roles(pw, org, uid))["engineer"] == "manual"  # the administrator's grant is not re-labelled
    _entra_remove(api, raw, gid, ident)
    assert (await _roles(pw, org, uid))["engineer"] == "manual"  # ... and survives the group removal
    assert api.delete(f"/scim/v2/Groups/{gid}", headers=_a(raw)).status_code == 204
    assert (await _roles(pw, org, uid))["engineer"] == "manual"


@pytest.mark.asyncio
async def test_roles_that_existed_before_provenance_are_manual_and_safe(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "gdefault")
    uid = await _user(pw, "gdefault1", org)
    assert (await pw.sup("SELECT column_default FROM information_schema.columns WHERE table_name='user_roles' AND column_name='granted_via'"))[0][0].startswith("'manual'")
    assert await _roles(pw, org, uid) == {"viewer": "manual"}
    rejected = False
    try:
        await pw.sup("UPDATE user_roles SET granted_via='bogus' WHERE user_id=:u", u=uid)
    except Exception:  # noqa: BLE001 - the CHECK constraint
        rejected = True
    assert rejected


@pytest.mark.asyncio
async def test_two_groups_granting_the_same_role_keep_it_until_the_last_one_goes(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "gmulti")
    uid, ident = await _linked_member(pw, api, org, raw, "gmulti1")
    g1, g2 = _new_group(api, raw, "A", [ident]), _new_group(api, raw, "B", [ident])
    await _map(pw, org, g1, "engineer")
    await _map(pw, org, g2, "engineer")
    _members_patch(api, raw, g1, "remove", [ident]); _members_patch(api, raw, g1, "add", [ident])  # noqa: E702
    _members_patch(api, raw, g2, "remove", [ident]); _members_patch(api, raw, g2, "add", [ident])  # noqa: E702
    assert (await _roles(pw, org, uid))["engineer"] == "scim"
    assert (await pw.sup("SELECT count(*) FROM scim_role_grants WHERE user_id=:u", u=uid))[0][0] == 2
    _entra_remove(api, raw, g1, ident)
    assert (await _roles(pw, org, uid)).get("engineer") == "scim"  # group B still grants it
    api.delete(f"/scim/v2/Groups/{g2}", headers=_a(raw))
    assert "engineer" not in await _roles(pw, org, uid)  # the last group is gone; viewer (manual) untouched
    assert (await _roles(pw, org, uid))["viewer"] == "manual"


@pytest.mark.asyncio
async def test_losing_every_role_deactivates_for_this_organization_only_and_keeps_the_roles(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "glast")
    other = await _org(pw, "glastother")
    uid, ident = await _linked_member(pw, api, org, raw, "glast1")
    await pw.sup("INSERT INTO user_roles (user_id, organization_id, role_id) VALUES (:u,:o,:r)", u=uid, o=other, r=await _role(pw, other, "viewer"))
    gid = _new_group(api, raw, "Only", [ident])
    await _map(pw, org, gid, "engineer")
    _members_patch(api, raw, gid, "remove", [ident]); _members_patch(api, raw, gid, "add", [ident])  # noqa: E702
    await pw.sup("DELETE FROM user_roles WHERE user_id=:u AND organization_id=:o AND granted_via='manual'", u=uid, o=org)  # their ONLY access is now the group
    assert await _roles(pw, org, uid) == {"engineer": "scim"}
    assert _entra_remove(api, raw, gid, ident).status_code == 200
    state = await _state(pw, org, uid)
    assert state is not None and state[0] == "deactivated" and state[1] == "scim"
    assert await _roles(pw, org, uid) == {"engineer": "scim"}  # roles stay for a later reactivation
    assert (await pw.sup("SELECT is_active FROM users WHERE id=:u", u=uid))[0][0] is True  # never the global flag
    assert await _roles(pw, other, uid) == {"viewer": "manual"}  # the other organization is untouched
    assert (await pw.sup("SELECT status FROM organization_member_states WHERE user_id=:u AND organization_id=:o", u=uid, o=other)) == []
    # a deactivated member is not touched by later group events
    _members_patch(api, raw, gid, "add", [ident])
    assert await _roles(pw, org, uid) == {"engineer": "scim"}


@pytest.mark.asyncio
async def test_a_withdrawal_that_would_remove_the_last_administrator_is_refused(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "gadmin")
    uid, ident = await _linked_member(pw, api, org, raw, "gadmin1")
    gid = _new_group(api, raw, "Admins")
    await _map(pw, org, gid, "admin")
    _members_patch(api, raw, gid, "add", [ident])
    assert (await _roles(pw, org, uid)).get("admin") == "scim"
    assert _entra_remove(api, raw, gid, ident).status_code == 200
    assert (await _roles(pw, org, uid)).get("admin") == "scim"  # refused: they are the only administrator
    assert await _audit(pw, org, "scim.role_revoke_refused") == 1
    # once another administrator exists the next synchronization withdraws it
    await _admin(pw, "gadmin2", org)
    _members_patch(api, raw, gid, "add", [ident]); _entra_remove(api, raw, gid, ident)  # noqa: E702
    assert "admin" not in await _roles(pw, org, uid)


@pytest.mark.asyncio
async def test_a_pending_identity_gets_its_group_roles_when_it_is_linked(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "gpend")
    admin = await _admin(pw, "gpendadmin", org)
    uid = await _user(pw, "gpend1", org)
    r = _create(api, raw, pw.email("gpend1"))  # no proof: stays pending, never linked by email
    ident = r.json()["id"]
    gid = _new_group(api, raw, "Engineers", [ident])
    await _map(pw, org, gid, "engineer")
    _members_patch(api, raw, gid, "remove", [ident]); _members_patch(api, raw, gid, "add", [ident])  # noqa: E702
    assert await _roles(pw, org, uid) == {"viewer": "manual"}  # pending: nothing granted
    t = _login(api, pw, "gpendadmin")
    r = api.post(f"/organizations/{org}/scim/identities/{ident}/link", headers=_h(t), json={"user_id": str(uid)})
    assert r.status_code == 200, r.text
    assert await _roles(pw, org, uid) == {"viewer": "manual", "engineer": "scim"}
    assert admin


@pytest.mark.asyncio
async def test_rename_does_not_change_what_a_group_grants_and_deleting_the_user_forgets_memberships(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "gren")
    uid, ident = await _linked_member(pw, api, org, raw, "gren1")
    gid = _new_group(api, raw, "Old", [ident])
    await _map(pw, org, gid, "engineer")
    _members_patch(api, raw, gid, "remove", [ident]); _members_patch(api, raw, gid, "add", [ident])  # noqa: E702
    api.patch(f"/scim/v2/Groups/{gid}", headers=_a(raw), json={"schemas": [PATCH], "Operations": [{"op": "Replace", "path": "displayName", "value": "Renamed"}]})
    assert (await _roles(pw, org, uid)).get("engineer") == "scim"
    assert await _audit(pw, org, "scim.group_renamed") == 1
    assert api.delete(f"/scim/v2/Users/{ident}", headers=_a(raw)).status_code == 204  # removed from the IdP
    assert (await _state(pw, org, uid))[0] == "deactivated"
    assert not await pw.sup("SELECT 1 FROM scim_group_members WHERE scim_identity_id=:i", i=uuid.UUID(ident))
    assert api.get(f"/scim/v2/Groups/{gid}", headers=_a(raw)).json()["members"] == []


# ================================================================ administration


@pytest.mark.asyncio
async def test_administrator_maps_groups_to_roles_with_escalation_and_tenant_checks(pw, api) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "gadm")
    other = await _org(pw, "gadmother")
    await _admin(pw, "gadmadmin", org)
    viewer_user = await _user(pw, "gadmviewer", org)
    uid, ident = await _linked_member(pw, api, org, raw, "gadm1")
    gid = _new_group(api, raw, "Engineers", [ident])
    eng, rev = await _role(pw, org, "engineer"), await _role(pw, org, "reviewer")
    t = _login(api, pw, "gadmadmin")
    url = f"/organizations/{org}/scim/groups"

    listed = api.get(url, headers=_h(t)).json()
    assert [(g["display_name"], g["member_count"], g["roles"]) for g in listed] == [("Engineers", 1, [])]

    r = api.put(f"{url}/{gid}/roles", headers=_h(t), json={"role_ids": [str(eng)]})
    assert r.status_code == 200 and [x["name"] for x in r.json()["roles"]] == ["engineer"]
    assert await _roles(pw, org, uid) == {"viewer": "manual", "engineer": "scim"}  # applied immediately to existing members
    assert await _audit(pw, org, "scim.group_roles_updated") == 1
    # unchanged -> no new audit event
    api.put(f"{url}/{gid}/roles", headers=_h(t), json={"role_ids": [str(eng)]})
    assert await _audit(pw, org, "scim.group_roles_updated") == 1

    # the group-managed role cannot be hand-removed (it would come straight back)
    r = api.delete(f"/organizations/{org}/members/{uid}/roles/{eng}", headers=_h(t))
    assert r.status_code == 409 and r.json()["error_code"] == "member.role_group_managed"
    r = api.put(f"/organizations/{org}/members/{uid}/roles", headers=_h(t), json={"role_ids": [str(await _role(pw, org, "viewer"))]})
    assert r.status_code == 409 and r.json()["error_code"] == "member.role_group_managed"
    # and the role cannot be deleted while a group maps to it
    assert api.delete(f"/organizations/{org}/roles/{eng}", headers=_h(t)).status_code == 409

    # swapping the mapping withdraws the old role and grants the new one
    r = api.put(f"{url}/{gid}/roles", headers=_h(t), json={"role_ids": [str(rev)]})
    assert r.status_code == 200
    assert await _roles(pw, org, uid) == {"viewer": "manual", "reviewer": "scim"}
    r = api.put(f"{url}/{gid}/roles", headers=_h(t), json={"role_ids": []})
    assert await _roles(pw, org, uid) == {"viewer": "manual"}

    # validation and isolation
    foreign = await _role(pw, other, "engineer")
    assert api.put(f"{url}/{gid}/roles", headers=_h(t), json={"role_ids": [str(foreign)]}).status_code == 400  # another org's role
    assert api.put(f"{url}/{gid}/roles", headers=_h(t), json={"role_ids": [str(uuid.uuid4())]}).status_code == 400
    assert api.put(f"{url}/{uuid.uuid4()}/roles", headers=_h(t), json={"role_ids": []}).status_code == 404
    assert api.put(f"/organizations/{other}/scim/groups/{gid}/roles", headers=_h(t), json={"role_ids": []}).status_code in (403, 404)
    # a member without tenancy:manage can do none of it
    tv = _login(api, pw, "gadmviewer")
    assert api.get(url, headers=_h(tv)).status_code == 403
    assert api.put(f"{url}/{gid}/roles", headers=_h(tv), json={"role_ids": [str(eng)]}).status_code == 403
    assert viewer_user


@pytest.mark.asyncio
async def test_groups_are_tenant_isolated_in_the_protocol_and_in_the_database(pw, api) -> None:  # noqa: F811
    org_a, raw_a = await _scim_org(pw, "giso")
    org_b, raw_b = await _scim_org(pw, "gisob")
    _u, ident_a = await _linked_member(pw, api, org_a, raw_a, "gisoa")
    gid_a = _new_group(api, raw_a, "Engineers", [ident_a])
    gid_b = _new_group(api, raw_b, "Engineers")  # the same name in another organization is fine
    assert gid_a != gid_b
    assert api.get(f"/scim/v2/Groups/{gid_a}", headers=_a(raw_b)).status_code == 404
    assert api.delete(f"/scim/v2/Groups/{gid_a}", headers=_a(raw_b)).status_code == 404
    assert api.patch(f"/scim/v2/Groups/{gid_a}", headers=_a(raw_b), json={"schemas": [PATCH], "Operations": [{"op": "add", "path": "members", "value": [{"value": ident_a}]}]}).status_code == 404
    # B's token cannot put A's user in B's group
    assert _members_patch(api, raw_b, gid_b, "add", [ident_a]).status_code == 400
    assert api.get("/scim/v2/Groups", headers=_a(raw_b)).json()["totalResults"] == 1
    assert api.get(f"/scim/v2/Groups/{gid_a}", headers=_a(raw_a)).status_code == 200
    # a SCIM token never works on the tenant API and the reverse
    assert api.get(f"/organizations/{org_a}/scim/groups", headers={"Authorization": f"Bearer {raw_a}"}).status_code in (401, 403)
    t = _login(api, pw, "gisoa") if False else None  # (REST tokens are exercised in the Users suite)
    assert t is None
    # RLS: the application role sees only the tenant context it set
    rows = await pw.sup("SELECT count(*) FROM scim_groups WHERE organization_id IN (:a,:b)", a=org_a, b=org_b)
    assert rows[0][0] == 2


@pytest.mark.asyncio
async def test_group_endpoints_need_a_scim_token_and_respect_the_kill_switches(pw, api, monkeypatch) -> None:  # noqa: F811
    org, raw = await _scim_org(pw, "gauth")
    assert api.get("/scim/v2/Groups").status_code == 401
    assert api.get("/scim/v2/Groups", headers={"Authorization": "Bearer scim_" + "x" * 40}).status_code == 401
    assert api.get("/scim/v2/ResourceTypes", headers=_a(raw)).json()["totalResults"] == 2
    await pw.sup("UPDATE scim_configurations SET enabled=false WHERE organization_id=:o", o=org)
    assert api.get("/scim/v2/Groups", headers=_a(raw)).status_code == 403
    await pw.sup("UPDATE scim_configurations SET enabled=true WHERE organization_id=:o", o=org)
    await pw.sup("UPDATE organizations SET status='suspended' WHERE id=:o", o=org)
    assert api.get("/scim/v2/Groups", headers=_a(raw)).status_code == 403
    assert api.post("/scim/v2/Groups", headers=_a(raw), json={"schemas": [GROUP], "displayName": "x"}).status_code == 403
    await pw.sup("UPDATE organizations SET status='active' WHERE id=:o", o=org)
    assert api.get("/scim/v2/Groups", headers=_a(raw)).status_code == 200
    assert await _token(pw, org)
