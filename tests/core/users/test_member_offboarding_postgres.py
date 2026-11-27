"""Organization-scoped member offboarding against REAL PostgreSQL as the real `ekip_app` role: the state
table and its trigger, row-level security, the membership service, and immediate enforcement on every path
that issues or accepts an organization-scoped session (REST identity, MCP, login, refresh, selection,
switching, SSO).

Skipped unless EKIP_TEST_POSTGRES_URL / EKIP_TEST_APP_URL point at a LOCAL migrated database.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.auth import repository as auth_repository
from app.core.auth import service as auth_service
from app.core.auth.schemas import (
    OrganizationSelectionRequest,
    RefreshRequest,
    SwitchOrganizationRequest,
    VerifiedIdPClaims,
)
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.core.users import membership
from app.core.users import service as users_service
from app.mcp.auth import resolve_mcp_identity
from app.mcp.oauth.constants import MCP_SCOPE, mcp_resource_identifier
from tests.core.auth.test_password_reset_postgres import (  # noqa: F401  (fixtures + helpers)
    OLD_PW,
    _APP_URL,
    _SUPER_URL,
    _expect,
    _identity,
    _is_local,
    _login,
    _org,
    _session,
    _user,
    env,
    pw,
)

pytestmark = pytest.mark.skipif(
    not (_SUPER_URL and _APP_URL and _is_local(_SUPER_URL) and _is_local(_APP_URL)),
    reason="needs EKIP_TEST_POSTGRES_URL and EKIP_TEST_APP_URL pointing at a LOCAL migrated database",
)

DEACTIVATED = "auth.membership_deactivated"
REVOKED = "auth.session_revoked"


@pytest_asyncio.fixture(autouse=True)
async def _migrated(pw):  # noqa: F811
    if not await pw.sup("SELECT 1 FROM information_schema.tables WHERE table_name='organization_member_states'"):
        pytest.skip("run `alembic upgrade head` on this database")
    yield


async def _admin(env, name, *orgs):  # noqa: F811
    """A tenant administrator (holds tenancy:manage) of `orgs`."""
    uid = await _user(env, name, *orgs)
    for org in orgs:
        await env.sup(
            "INSERT INTO user_roles (user_id, organization_id, role_id) SELECT :u, :o, id FROM roles "
            "WHERE name='admin' AND organization_id IS NULL ON CONFLICT DO NOTHING",
            u=uid, o=org,
        )
    return uid


async def _actor(env, admin, org):  # noqa: F811
    return await env.run(org, lambda s: users_service.resolve_identity(s, admin, org))


async def _deactivate(env, admin, org, target, reason="left the company"):  # noqa: F811
    actor = await _actor(env, admin, org)
    return await env.run(org, lambda s: membership.deactivate_member(s, actor, org, target, reason=reason))


async def _reactivate(env, admin, org, target, reason="rehired"):  # noqa: F811
    actor = await _actor(env, admin, org)
    return await env.run(org, lambda s: membership.reactivate_member(s, actor, org, target, reason=reason))


async def _state(env, org, user):  # noqa: F811
    rows = await env.sup(
        "SELECT status, source, reason, member_epoch, deactivated_at IS NOT NULL FROM organization_member_states "
        "WHERE organization_id=:o AND user_id=:u", o=org, u=user,
    )
    return rows[0] if rows else None


async def _live_refresh(env, org, user=None):  # noqa: F811
    if user is None:
        return (await env.sup("SELECT count(*) FROM refresh_tokens WHERE organization_id=:o AND revoked_at IS NULL", o=org))[0][0]
    return (await env.sup("SELECT count(*) FROM refresh_tokens WHERE organization_id=:o AND user_id=:u AND revoked_at IS NULL", o=org, u=user))[0][0]


# ============================================================ the default and the lifecycle


@pytest.mark.asyncio
async def test_an_existing_membership_with_no_state_row_is_active(pw) -> None:  # noqa: F811
    org = await _org(pw, "dflt")
    admin = await _admin(pw, "dfltadmin", org)
    member = await _user(pw, "dfltmember", org)
    assert await _state(pw, org, member) is None  # nothing was backfilled
    actor = await _actor(pw, admin, org)
    status = await pw.run(org, lambda s: membership.get_member_status(s, actor, org, member))
    assert (status.status, status.member_epoch, status.source, status.reason) == ("active", 0, None, None)
    assert (await _identity(pw, await _session(pw, member, org))).user_id == member


@pytest.mark.asyncio
async def test_deactivate_and_reactivate_record_state_epoch_and_audit(pw) -> None:  # noqa: F811
    org = await _org(pw, "life")
    admin = await _admin(pw, "lifeadmin", org)
    member = await _user(pw, "lifemember", org)

    after = await _deactivate(pw, admin, org, member, reason="left the company")
    assert (after.status, after.source, after.reason, after.member_epoch) == ("deactivated", "admin", "left the company", 1)
    assert after.deactivated_at is not None
    assert await _state(pw, org, member) == ("deactivated", "admin", "left the company", 1, True)

    back = await _reactivate(pw, admin, org, member, reason="rehired")
    assert (back.status, back.member_epoch, back.deactivated_at) == ("active", 2, None)
    assert await _state(pw, org, member) == ("active", "admin", "rehired", 2, False)

    audit = await pw.sup(
        "SELECT action, actor, resource_id::text, metadata->>'source', metadata->>'reason', metadata->>'member_epoch' "
        "FROM audit_logs WHERE organization_id=:o AND action LIKE 'member.%' AND action IN ('member.deactivate','member.reactivate') ORDER BY occurred_at",
        o=org,
    )
    assert audit == [
        ("member.deactivate", f"user:{admin}", str(member), "admin", "left the company", "1"),
        ("member.reactivate", f"user:{admin}", str(member), "admin", "rehired", "2"),
    ]


@pytest.mark.asyncio
async def test_deactivation_is_per_organization_and_deletes_nothing(pw) -> None:  # noqa: F811
    a, b = await _org(pw, "pa"), await _org(pw, "pb")
    admin = await _admin(pw, "padmin", a)
    shared = await _user(pw, "shared", a, b)
    in_a, in_b = await _session(pw, shared, a), await _session(pw, shared, b)
    projects_before = (await pw.sup("SELECT count(*) FROM projects WHERE organization_id=:o", o=a))[0][0]
    roles_before = (await pw.sup("SELECT count(*) FROM user_roles WHERE organization_id=:o AND user_id=:u", o=a, u=shared))[0][0]

    await _deactivate(pw, admin, a, shared)

    # the GLOBAL account is untouched, so is every other organization, and nothing was deleted
    assert (await pw.sup("SELECT is_active FROM users WHERE id=:u", u=shared))[0][0] is True
    assert (await pw.sup("SELECT count(*) FROM projects WHERE organization_id=:o", o=a))[0][0] == projects_before
    assert (await pw.sup("SELECT count(*) FROM user_roles WHERE organization_id=:o AND user_id=:u", o=a, u=shared))[0][0] == roles_before
    assert await _state(pw, b, shared) is None
    assert (await _identity(pw, in_b)).organization_id == b
    assert await _live_refresh(pw, b, shared) == 1  # B's refresh token was not revoked
    assert (await pw.run(None, lambda s: auth_service.refresh(s, RefreshRequest(refresh_token=in_b.refresh_token)))).access_token
    await _expect(_identity(pw, in_a), PermissionDeniedError, DEACTIVATED)


# ============================================================ immediate access enforcement


@pytest.mark.asyncio
async def test_an_access_token_issued_before_deactivation_fails_on_the_next_request(pw) -> None:  # noqa: F811
    org = await _org(pw, "tok")
    admin = await _admin(pw, "tokadmin", org)
    member = await _user(pw, "tokmember", org)
    live = await _session(pw, member, org)
    assert (await _identity(pw, live)).user_id == member
    await _deactivate(pw, admin, org, member)
    await _expect(_identity(pw, live), PermissionDeniedError, DEACTIVATED)


@pytest.mark.asyncio
async def test_old_tokens_stay_dead_after_reactivation_and_a_fresh_sign_in_works(pw) -> None:  # noqa: F811
    org = await _org(pw, "react")
    admin = await _admin(pw, "reactadmin", org)
    member = await _user(pw, "reactmember", org)
    old = await _session(pw, member, org)
    assert auth_service.verify_access_token(old.access_token).member_epoch == 0
    await _deactivate(pw, admin, org, member)
    await _reactivate(pw, admin, org, member)

    await _expect(_identity(pw, old), PermissionDeniedError, REVOKED)  # not revived
    await _expect(
        pw.run(None, lambda s: auth_service.refresh(s, RefreshRequest(refresh_token=old.refresh_token))),
        PermissionDeniedError,
        "auth.refresh_token_reused",
    )
    fresh = await _login(pw, "reactmember", OLD_PW)  # reactivation requires fresh authentication
    assert auth_service.verify_access_token(fresh.access_token).member_epoch == 2
    assert (await _identity(pw, fresh)).user_id == member
    await _expect(_identity(pw, old), PermissionDeniedError, REVOKED)  # still dead


@pytest.mark.asyncio
async def test_reactivation_issues_no_session_by_itself(pw) -> None:  # noqa: F811
    org = await _org(pw, "nosess")
    admin = await _admin(pw, "nosessadmin", org)
    member = await _user(pw, "nosessmember", org)
    await _session(pw, member, org)
    await _deactivate(pw, admin, org, member)
    await _reactivate(pw, admin, org, member)
    assert await _live_refresh(pw, org, member) == 0


@pytest.mark.asyncio
async def test_deactivation_revokes_every_refresh_token_of_that_person_in_that_organization_only(pw) -> None:  # noqa: F811
    a, b = await _org(pw, "ra"), await _org(pw, "rb")
    admin = await _admin(pw, "radmin", a)
    shared = await _user(pw, "rshared", a, b)
    other = await _user(pw, "rother", a)
    for _ in range(3):
        await _session(pw, shared, a)
    await _session(pw, shared, b)
    await _session(pw, other, a)
    assert await _live_refresh(pw, a, shared) == 3
    await _deactivate(pw, admin, a, shared)
    assert await _live_refresh(pw, a, shared) == 0
    assert await _live_refresh(pw, b, shared) == 1  # the same person's other organization
    assert await _live_refresh(pw, a, other) == 1  # a colleague in the same organization


@pytest.mark.asyncio
async def test_refresh_by_a_deactivated_member_reports_the_real_reason_not_a_reuse_alarm(pw) -> None:  # noqa: F811
    org = await _org(pw, "ref")
    admin = await _admin(pw, "refadmin", org)
    member = await _user(pw, "refmember", org)
    tokens = await _session(pw, member, org)
    await _deactivate(pw, admin, org, member)
    await _expect(
        pw.run(None, lambda s: auth_service.refresh(s, RefreshRequest(refresh_token=tokens.refresh_token))),
        PermissionDeniedError,
        DEACTIVATED,
    )


@pytest.mark.asyncio
async def test_no_session_can_be_minted_for_a_deactivated_member_by_any_route(pw) -> None:  # noqa: F811
    a, b = await _org(pw, "la"), await _org(pw, "lb")
    admin = await _admin(pw, "ladmin", a)
    only_a = await _user(pw, "onlya", a)
    both = await _user(pw, "both", a, b)
    picker = await _login(pw, "both", OLD_PW)
    assert picker.status == "organization_selection_required"
    in_b = await _session(pw, both, b)
    await _deactivate(pw, admin, a, only_a)
    await _deactivate(pw, admin, a, both)

    # password login: the only organization is deactivated -> a specific error, no session
    await _expect(_login(pw, "onlya", OLD_PW), PermissionDeniedError, DEACTIVATED)
    # a person with another organization goes straight into it (A is neither offered nor selected)
    direct = await _login(pw, "both", OLD_PW)
    assert direct.status == "complete" and (await _identity(pw, direct)).organization_id == b
    # a stale picker cannot select it, and switching cannot reach it
    await _expect(
        pw.run(None, lambda s: auth_service.select_organization(s, OrganizationSelectionRequest(selection_token=picker.selection_token, organization_id=a))),
        PermissionDeniedError,
        DEACTIVATED,
    )
    await _expect(
        pw.run(None, lambda s: auth_service.switch_organization(s, user_id=both, data=SwitchOrganizationRequest(organization_id=a))),
        PermissionDeniedError,
        DEACTIVATED,
    )
    # the organization list never offers it
    listing = await pw.run(None, lambda s: auth_service.list_available_organizations(s, both))
    assert [o.id for o in listing.organizations] == [b]
    # ... and `_issue_session` itself (the one choke point) refuses, whoever calls it
    await _expect(_session(pw, both, a), PermissionDeniedError, DEACTIVATED)
    assert in_b


@pytest.mark.asyncio
async def test_sso_refuses_a_deactivated_member_even_though_the_idp_still_vouches_for_them(pw) -> None:  # noqa: F811
    org = await _org(pw, "sso")
    admin = await _admin(pw, "ssoadmin", org)
    member = await _user(pw, "ssomember", org, password=None)
    subject = f"idp-{uuid.uuid4().hex[:8]}"
    await pw.run(org, lambda s: auth_repository.insert_external_identity_mapping(s, organization_id=org, user_id=member, idp_subject=subject))
    claims = VerifiedIdPClaims(sub=subject, email=pw.email("ssomember"), email_verified=True, name="S", groups=())

    async def attempt():
        return await pw.run(
            org, lambda s: auth_service._resolve_or_provision_user(s, organization_id=org, idp_subject=subject, idp_claims=claims)
        )

    assert await attempt() == member  # works while active
    await _deactivate(pw, admin, org, member)
    await _expect(attempt(), PermissionDeniedError, DEACTIVATED)
    roles = (await pw.sup("SELECT count(*) FROM user_roles WHERE organization_id=:o AND user_id=:u", o=org, u=member))[0][0]
    await _reactivate(pw, admin, org, member)
    assert await attempt() == member
    assert roles >= 1


@pytest.mark.asyncio
async def test_a_matching_provisioning_rule_does_not_readmit_a_deactivated_member(pw) -> None:  # noqa: F811
    org = await _org(pw, "rule")
    admin = await _admin(pw, "ruleadmin", org)
    member = await _user(pw, "rulemember", org, password=None)
    viewer = (await pw.sup("SELECT id FROM roles WHERE name='viewer' AND organization_id=:o", o=org))[0][0]
    await pw.sup(
        "INSERT INTO organization_access_rules (organization_id, rule_type, value, grants_role_id, is_active) VALUES (:o,'domain','example.invalid',:r,true)",
        o=org, r=viewer,
    )
    await _deactivate(pw, admin, org, member)
    subject = f"idp-{uuid.uuid4().hex[:8]}"  # no mapping yet: goes through the provisioning rules
    claims = VerifiedIdPClaims(sub=subject, email=pw.email("rulemember"), email_verified=True, name="R", groups=())
    await _expect(
        pw.run(org, lambda s: auth_service._resolve_or_provision_user(s, organization_id=org, idp_subject=subject, idp_claims=claims)),
        PermissionDeniedError,
        DEACTIVATED,
    )
    assert (await pw.sup("SELECT count(*) FROM external_identity_mappings WHERE organization_id=:o AND idp_subject=:s", o=org, s=subject))[0][0] == 0


# ============================================================ MCP


@pytest.mark.asyncio
async def test_mcp_tool_calls_and_oauth_grants_follow_the_membership(pw) -> None:  # noqa: F811
    org = await _org(pw, "mcp")
    admin = await _admin(pw, "mcpadmin", org)
    member = await _user(pw, "mcpmember", org)
    resource = mcp_resource_identifier()

    def mcp_token(epoch):
        token, _i, _e = auth_service._issue_access_token(
            member, org, scope=MCP_SCOPE, audience=resource, issuer=resource, member_epoch=epoch
        )
        return token

    old = mcp_token(0)
    assert (await pw.run(org, lambda s: resolve_mcp_identity(s, old))).user_id == member

    # an MCP OAuth session exactly as the provider mints it (bound to a client, MCP audience)
    oauth = await pw.run(
        org,
        lambda s: auth_service._issue_session(
            s, user_id=member, organization_id=org, family_id=uuid.uuid4(), scope=MCP_SCOPE,
            oauth_client_id="client-1", audience=resource, issuer=resource,
        ),
    )
    assert (await pw.run(org, lambda s: resolve_mcp_identity(s, oauth.access_token))).user_id == member

    await _deactivate(pw, admin, org, member)

    await _expect(pw.run(org, lambda s: resolve_mcp_identity(s, old)), PermissionDeniedError, DEACTIVATED)
    await _expect(pw.run(org, lambda s: resolve_mcp_identity(s, oauth.access_token)), PermissionDeniedError, DEACTIVATED)
    # the OAuth refresh token (a refresh_tokens row bound to the client) was revoked with the rest
    assert (await pw.sup("SELECT count(*) FROM refresh_tokens WHERE organization_id=:o AND oauth_client_id='client-1' AND revoked_at IS NULL", o=org))[0][0] == 0
    await _expect(
        pw.run(
            None,
            lambda s: auth_service.refresh(
                s, RefreshRequest(refresh_token=oauth.refresh_token), scope=MCP_SCOPE, oauth_client_id="client-1",
                audience=resource, issuer=resource,
            ),
        ),
        PermissionDeniedError,
        DEACTIVATED,
    )
    # a not-yet-exchanged authorization code (held by the provider) carries a session minted BEFORE this
    # moment: its access token carries the old epoch and its refresh token is revoked, so it is worthless
    await _expect(pw.run(org, lambda s: resolve_mcp_identity(s, oauth.access_token)), PermissionDeniedError, DEACTIVATED)
    # nor can a NEW grant be minted for them
    await _expect(
        pw.run(org, lambda s: auth_service._issue_session(
            s, user_id=member, organization_id=org, family_id=uuid.uuid4(), scope=MCP_SCOPE,
            oauth_client_id="client-1", audience=resource, issuer=resource)),
        PermissionDeniedError,
        DEACTIVATED,
    )

    await _reactivate(pw, admin, org, member)
    await _expect(pw.run(org, lambda s: resolve_mcp_identity(s, old)), PermissionDeniedError, REVOKED)
    await _expect(pw.run(org, lambda s: resolve_mcp_identity(s, oauth.access_token)), PermissionDeniedError, REVOKED)
    fresh = mcp_token(2)
    assert (await pw.run(org, lambda s: resolve_mcp_identity(s, fresh))).user_id == member


# ============================================================ safeguards


@pytest.mark.asyncio
async def test_the_last_administrator_cannot_be_deactivated(pw) -> None:  # noqa: F811
    org = await _org(pw, "last")
    admin1 = await _admin(pw, "lastadmin1", org)
    admin2 = await _admin(pw, "lastadmin2", org)

    await _deactivate(pw, admin1, org, admin2)  # a second admin remains (admin1): fine
    # admin2 no longer counts as an administrator, so admin1 is now the last one
    with pytest.raises(ConflictError) as info:
        await pw.run(org, lambda s: users_service.assert_organization_keeps_an_admin(s, org, admin1, lock=False))
    assert info.value.error_code == "org.last_admin"
    await _reactivate(pw, admin1, org, admin2)
    await pw.run(org, lambda s: users_service.assert_organization_keeps_an_admin(s, org, admin1, lock=False))  # two again


@pytest.mark.asyncio
async def test_deactivating_the_last_admin_is_refused_when_another_admin_acts(pw) -> None:  # noqa: F811
    """The actor must hold tenancy:manage, so build an organization whose only ADMIN-role holder is the
    target while another account holds a custom role with tenancy:manage -- then deactivate_member's
    last-admin guard is the thing that refuses (and the failed attempt leaves nothing behind)."""
    org = await _org(pw, "last2")
    target = await _admin(pw, "last2target", org)
    actor_user = await _user(pw, "last2actor", org)
    role_id = (await pw.sup("INSERT INTO roles (organization_id, name, description) VALUES (:o,'managers','x') RETURNING id", o=org))[0][0]
    await pw.sup(
        "INSERT INTO role_permissions (role_id, permission_id) SELECT :r, id FROM permissions WHERE code='tenancy:manage'", r=role_id
    )
    await pw.sup("INSERT INTO user_roles (user_id, organization_id, role_id) VALUES (:u,:o,:r)", u=actor_user, o=org, r=role_id)
    # two admins by definition (target and actor) -> deactivating the target is allowed...
    actor = await _actor(pw, actor_user, org)
    await pw.run(org, lambda s: membership.deactivate_member(s, actor, org, target, reason="r"))
    await pw.run(org, lambda s: membership.reactivate_member(s, actor, org, target, reason="r"))
    # ...but take the actor's own role away from the admin set by deactivating the actor's peer first:
    # with the actor the only admin left, deactivating the target (the other admin) is still allowed, yet
    # making the target the last one standing is refused.
    await pw.sup("UPDATE users SET is_active=false WHERE id=:u", u=actor_user)  # the actor stops counting
    with pytest.raises(ConflictError) as info:
        await pw.run(org, lambda s: membership.deactivate_member(s, actor, org, target, reason="r"))
    assert info.value.error_code == "org.last_admin"
    assert await _state(pw, org, target) == ("active", "admin", "r", 2, False)  # unchanged by the refused attempt
    await pw.sup("UPDATE users SET is_active=true WHERE id=:u", u=actor_user)


@pytest.mark.asyncio
async def test_self_duplicate_and_missing_state_are_refused(pw) -> None:  # noqa: F811
    org = await _org(pw, "guard")
    admin = await _admin(pw, "guardadmin", org)
    admin2 = await _admin(pw, "guardadmin2", org)
    member = await _user(pw, "guardmember", org)
    with pytest.raises(ConflictError) as info:
        await _deactivate(pw, admin, org, admin)
    assert info.value.error_code == "member.cannot_deactivate_self"
    await _deactivate(pw, admin, org, member)
    with pytest.raises(ConflictError) as info:
        await _deactivate(pw, admin2, org, member)
    assert info.value.error_code == "member.already_deactivated"
    with pytest.raises(ConflictError) as info:
        await _reactivate(pw, admin, org, admin2)  # never deactivated
    assert info.value.error_code == "member.not_deactivated"
    await _reactivate(pw, admin, org, member)
    with pytest.raises(ConflictError) as info:
        await _reactivate(pw, admin, org, member)  # already active again
    assert info.value.error_code == "member.not_deactivated"


@pytest.mark.asyncio
async def test_unauthorized_users_cannot_change_or_read_a_membership(pw) -> None:  # noqa: F811
    org = await _org(pw, "authz")
    admin = await _admin(pw, "authzadmin", org)
    member = await _user(pw, "authzmember", org)
    victim = await _user(pw, "authzvictim", org)
    plain = await _actor(pw, member, org)  # a viewer: no tenancy:manage
    for call in (
        lambda s: membership.deactivate_member(s, plain, org, victim, reason="r"),
        lambda s: membership.reactivate_member(s, plain, org, victim, reason="r"),
        lambda s: membership.get_member_status(s, plain, org, victim),
    ):
        with pytest.raises(PermissionDeniedError):
            await pw.run(org, call)
    assert await _state(pw, org, victim) is None
    assert admin


@pytest.mark.asyncio
async def test_an_administrator_of_another_organization_cannot_touch_this_one(pw) -> None:  # noqa: F811
    a, b = await _org(pw, "xa"), await _org(pw, "xb")
    admin_a = await _admin(pw, "xadmina", a)
    victim = await _user(pw, "xvictim", b)
    actor = await _actor(pw, admin_a, a)
    # naming the other organization is refused outright ...
    with pytest.raises(PermissionDeniedError) as info:
        await pw.run(a, lambda s: membership.deactivate_member(s, actor, b, victim, reason="r"))
    assert info.value.error_code == "users.cross_organization_denied"
    # ... and naming the victim in the actor's OWN organization is the same 404 as an unknown user
    for target in (victim, uuid.uuid4()):
        with pytest.raises(NotFoundError) as info2:
            await pw.run(a, lambda s: membership.deactivate_member(s, actor, a, target, reason="r"))
        assert info2.value.error_code == "member.not_found"
    assert await _state(pw, b, victim) is None
    assert (await _identity(pw, await _session(pw, victim, b))).user_id == victim


# ============================================================ database rules: RLS, trigger, privileges


@pytest.mark.asyncio
async def test_row_level_security_confines_the_application_role_to_its_own_organization(pw) -> None:  # noqa: F811
    a, b = await _org(pw, "rlsa"), await _org(pw, "rlsb")
    admin_a, admin_b = await _admin(pw, "rlsadmina", a), await _admin(pw, "rlsadminb", b)
    mem_a, mem_b = await _user(pw, "rlsmema", a), await _user(pw, "rlsmemb", b)
    await _deactivate(pw, admin_a, a, mem_a)
    await _deactivate(pw, admin_b, b, mem_b)

    async def visible(s):
        return [r[0] for r in (await s.execute(text("SELECT organization_id FROM organization_member_states"))).all()]

    assert set(await pw.run(a, visible)) == {a}  # only its own row, with no WHERE clause at all
    assert set(await pw.run(b, visible)) == {b}

    async def touch_b(s):  # tenant context = A, statements name B
        update = await s.execute(text("UPDATE organization_member_states SET status='active' WHERE organization_id=:b"), {"b": b})
        return update.rowcount

    assert await pw.run(a, touch_b) == 0  # RLS hides B's rows from the UPDATE
    assert await _state(pw, b, mem_b) == ("deactivated", "admin", "left the company", 1, True)

    async def insert_into_b(s):
        await s.execute(
            text("INSERT INTO organization_member_states (organization_id, user_id, status) VALUES (:b, :u, 'deactivated')"),
            {"b": b, "u": mem_a},
        )

    with pytest.raises(DBAPIError) as info:
        await pw.run(a, insert_into_b)
    assert "row-level security" in str(info.value).lower()
    # with NO tenant context the application role sees nothing (an unset setting matches no row, and a
    # previously-reset one is rejected outright -- either way no other tenant's rows are ever returned)
    try:
        assert await pw.run(None, visible) == []
    except DBAPIError:
        pass


@pytest.mark.asyncio
async def test_the_database_computes_the_epoch_and_refuses_tampering(pw) -> None:  # noqa: F811
    org = await _org(pw, "trg")
    admin = await _admin(pw, "trgadmin", org)
    member = await _user(pw, "trgmember", org)

    # inserting with a forged epoch / timestamp: the trigger overrides both
    await pw.sup(
        "INSERT INTO organization_member_states (organization_id, user_id, status, member_epoch, deactivated_at) "
        "VALUES (:o, :u, 'deactivated', 99, NULL)", o=org, u=member,
    )
    assert await _state(pw, org, member) == ("deactivated", "admin", None, 1, True)
    # ... a non-status update cannot lower or raise the epoch
    await pw.sup("UPDATE organization_member_states SET member_epoch = 0, reason='x' WHERE organization_id=:o AND user_id=:u", o=org, u=member)
    assert (await _state(pw, org, member))[3] == 1
    # ... every status change moves it by exactly one
    await pw.sup("UPDATE organization_member_states SET status='active' WHERE organization_id=:o AND user_id=:u", o=org, u=member)
    await pw.sup("UPDATE organization_member_states SET status='deactivated' WHERE organization_id=:o AND user_id=:u", o=org, u=member)
    assert (await _state(pw, org, member))[3] == 3
    # ... the key cannot be re-pointed, and a direct delete (which would reset the epoch) is refused
    other = await _user(pw, "trgother", org)
    with pytest.raises(DBAPIError) as info:
        await pw.sup("UPDATE organization_member_states SET user_id=:x WHERE organization_id=:o AND user_id=:u", x=other, o=org, u=member)
    assert "cannot change" in str(info.value)
    with pytest.raises(DBAPIError) as info:
        await pw.sup("DELETE FROM organization_member_states WHERE organization_id=:o AND user_id=:u", o=org, u=member)
    assert "cannot be deleted directly" in str(info.value)
    # constraints
    for bad in ("status='suspended'", "source='manual'"):
        with pytest.raises(DBAPIError):
            await pw.sup(f"UPDATE organization_member_states SET {bad} WHERE organization_id=:o AND user_id=:u", o=org, u=member)
    assert admin


@pytest.mark.asyncio
async def test_the_application_role_cannot_delete_a_state_row_or_forge_an_epoch(pw) -> None:  # noqa: F811
    org = await _org(pw, "app")
    admin = await _admin(pw, "appadmin", org)
    member = await _user(pw, "appmember", org)
    await _deactivate(pw, admin, org, member)

    async def delete(s):
        await s.execute(text("DELETE FROM organization_member_states WHERE organization_id=:o AND user_id=:u"), {"o": org, "u": member})

    with pytest.raises(DBAPIError):  # as ekip_app, in its own organization: still refused by the trigger
        await pw.run(org, delete)

    async def forge(s):
        await s.execute(text("UPDATE organization_member_states SET member_epoch=0 WHERE organization_id=:o AND user_id=:u"), {"o": org, "u": member})

    await pw.run(org, forge)
    assert (await _state(pw, org, member))[3] == 1  # silently ignored: the database owns the epoch


@pytest.mark.asyncio
async def test_the_lookup_function_is_narrow_and_executable_only_by_the_application_role(pw) -> None:  # noqa: F811
    row = (
        await pw.sup(
            "SELECT p.prosecdef, p.proconfig::text, has_function_privilege('ekip_app', p.oid, 'EXECUTE'), "
            "has_function_privilege('public', p.oid, 'EXECUTE') FROM pg_proc p "
            "WHERE p.proname='member_deactivated_organization_ids'"
        )
    )[0]
    assert row[0] is True and "search_path=pg_catalog, pg_temp" in row[1] and row[2] is True and row[3] is False
    org = await _org(pw, "fn")
    admin = await _admin(pw, "fnadmin", org)
    member = await _user(pw, "fnmember", org)
    await _deactivate(pw, admin, org, member)

    async def ids(s):
        return {r[0] for r in (await s.execute(text("SELECT member_deactivated_organization_ids(:u)"), {"u": member})).all()}

    assert await pw.run(None, ids) == {org}  # works with no tenant context at all


@pytest.mark.asyncio
async def test_organization_suspension_is_unchanged_and_takes_precedence(pw) -> None:  # noqa: F811
    org = await _org(pw, "susp")
    admin = await _admin(pw, "suspadmin", org)
    member = await _user(pw, "suspmember", org)
    live = await _session(pw, member, org)
    await _deactivate(pw, admin, org, member)
    await pw.sup("UPDATE organizations SET status='suspended' WHERE id=:o", o=org)
    await _expect(_identity(pw, live), PermissionDeniedError, "auth.organization_suspended")
    await pw.sup("UPDATE organizations SET status='active' WHERE id=:o", o=org)
    # the organization's own epoch ended the old token (Phase 3); the membership state survived the cycle:
    # no new session can be minted for the deactivated member
    await _expect(_identity(pw, live), PermissionDeniedError, REVOKED)
    await _expect(_session(pw, member, org), PermissionDeniedError, DEACTIVATED)
    assert await _state(pw, org, member) == ("deactivated", "admin", "left the company", 1, True)
