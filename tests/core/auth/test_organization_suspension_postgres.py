"""Phase 1 of the organization lifecycle: a suspended organization is refused everywhere, at once,
and only for that organization -- against REAL PostgreSQL as the real `ekip_app` role.

Status is flipped with a direct superuser UPDATE because the lifecycle APIs do not exist yet
(Phase 5); what is under test is the ENFORCEMENT. Every organization here is created by
`create_organization`, i.e. in the default `onboarding` state -- which doubles as the proof that
`onboarding` organizations stay fully functional.

Skipped unless EKIP_TEST_POSTGRES_URL / EKIP_TEST_APP_URL point at a LOCAL migrated database.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.auth import email_verification
from app.core.auth import repository as auth_repository
from app.core.auth import service as auth_service
from app.core.auth.schemas import (
    OrganizationSelectionRequest,
    RefreshRequest,
    SessionTokens,
    SwitchOrganizationRequest,
)
from app.core.exceptions import PermissionDeniedError
from app.core.tenancy import service as tenancy_service
from app.core.tenancy.schemas import InvitationAcceptRequest, InvitationCreate
from app.core.users import service as users_service
from app.mcp.auth import resolve_mcp_identity
from app.mcp.oauth.constants import MCP_SCOPE, mcp_resource_identifier
from tests.core.auth.test_password_reset_postgres import (  # noqa: F401  (fixtures + helpers)
    NEW_PW,
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

SUSPENDED = "auth.organization_suspended"


async def _set_status(env, org, status):
    await env.sup("UPDATE organizations SET status=:s WHERE id=:o", s=status, o=org)


async def _live_refresh_tokens(env, org):
    return (await env.sup("SELECT count(*) FROM refresh_tokens WHERE organization_id=:o AND revoked_at IS NULL", o=org))[0][0]


async def _refresh(env, tokens):
    return await env.run(None, lambda s: auth_service.refresh(s, RefreshRequest(refresh_token=tokens.refresh_token)))


# ============================================================ existing access tokens


@pytest.mark.asyncio
async def test_new_organizations_are_onboarding_and_fully_functional(pw) -> None:  # noqa: F811
    org = await _org(pw, "onb")
    uid = await _user(pw, "onb", org)
    assert (await pw.sup("SELECT status FROM organizations WHERE id=:o", o=org))[0][0] == "onboarding"

    session = await _session(pw, uid, org)
    assert (await _identity(pw, session)).organization_id == org
    assert isinstance(await _login(pw, "onb", OLD_PW), SessionTokens)
    assert (await _refresh(pw, session)).access_token


@pytest.mark.asyncio
async def test_an_already_issued_access_token_is_refused_the_moment_the_organization_is_suspended(pw) -> None:  # noqa: F811
    org = await _org(pw, "tok")
    uid = await _user(pw, "tok", org)
    live = await _session(pw, uid, org)
    assert (await _identity(pw, live)).user_id == uid  # works, with ~60 minutes of life left

    await _set_status(pw, org, "suspended")

    await _expect(_identity(pw, live), PermissionDeniedError, SUSPENDED)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["Suspended", " SUSPENDED ", "trial", ""])
async def test_since_phase_3_the_database_refuses_any_other_spelling_or_value(pw, status) -> None:  # noqa: F811
    """Phase 1 matched the stored text case- and whitespace-insensitively because nothing constrained
    it. The CHECK constraint now makes those variants unrepresentable (the normalizer stays as
    defense in depth). The trigger fires first for a differing value, so either error is acceptable --
    what matters is that nothing but the three exact values can be stored."""
    from sqlalchemy.exc import DBAPIError

    org = await _org(pw, "case")
    with pytest.raises(DBAPIError):
        await _set_status(pw, org, status)
    assert (await pw.sup("SELECT status FROM organizations WHERE id=:o", o=org))[0][0] == "onboarding"


@pytest.mark.asyncio
async def test_active_and_onboarding_do_not_block_and_do_not_end_sessions(pw) -> None:  # noqa: F811
    org = await _org(pw, "stat")
    uid = await _user(pw, "stat", org)
    live = await _session(pw, uid, org)
    await _set_status(pw, org, "active")  # onboarding -> active: no session effect at all
    assert (await _identity(pw, live)).user_id == uid


@pytest.mark.asyncio
async def test_suspending_one_organization_leaves_the_same_users_other_organization_alone(pw) -> None:  # noqa: F811
    a, b = await _org(pw, "ma"), await _org(pw, "mb")
    uid = await _user(pw, "multi", a, b)
    in_a, in_b = await _session(pw, uid, a), await _session(pw, uid, b)

    await _set_status(pw, a, "suspended")

    await _expect(_identity(pw, in_a), PermissionDeniedError, SUSPENDED)
    assert (await _identity(pw, in_b)).organization_id == b
    assert (await _refresh(pw, in_b)).access_token  # the other organization's session still rotates
    await _expect(_refresh(pw, in_a), PermissionDeniedError, SUSPENDED)


# ============================================================ every authentication entry point


@pytest.mark.asyncio
async def test_refresh_is_refused_and_the_token_is_not_consumed(pw) -> None:  # noqa: F811
    org = await _org(pw, "ref")
    uid = await _user(pw, "ref", org)
    tokens = await _session(pw, uid, org)
    await _set_status(pw, org, "suspended")

    await _expect(_refresh(pw, tokens), PermissionDeniedError, SUSPENDED)

    # Phase 3: the suspension itself revoked the organization's refresh tokens, so the presenter gets the
    # real reason (not a "reuse" signal) and nothing is minted.
    assert await _live_refresh_tokens(pw, org) == 0


@pytest.mark.asyncio
async def test_login_selection_and_switching_never_reach_a_suspended_organization(pw) -> None:  # noqa: F811
    a, b = await _org(pw, "la"), await _org(pw, "lb")
    only_a = await _user(pw, "onlya", a)
    both = await _user(pw, "both", a, b)
    picker = await _login(pw, "both", OLD_PW)
    assert picker.status == "organization_selection_required"
    existing = await _session(pw, both, b)
    await _set_status(pw, a, "suspended")

    # a user whose ONLY organization is suspended gets a specific error and no session
    await _expect(_login(pw, "onlya", OLD_PW), PermissionDeniedError, SUSPENDED)
    # a user with another organization goes straight into it
    direct = await _login(pw, "both", OLD_PW)
    assert direct.status == "complete" and (await _identity(pw, direct)).organization_id == b
    # a stale picker cannot select it, nor can an authenticated user switch into it
    await _expect(
        pw.run(None, lambda s: auth_service.select_organization(s, OrganizationSelectionRequest(selection_token=picker.selection_token, organization_id=a))),
        PermissionDeniedError,
        SUSPENDED,
    )
    await _expect(
        pw.run(None, lambda s: auth_service.switch_organization(s, user_id=both, data=SwitchOrganizationRequest(organization_id=a))),
        PermissionDeniedError,
        SUSPENDED,
    )
    listed = await pw.run(None, lambda s: auth_service.list_available_organizations(s, both))
    assert {o.id for o in listed.organizations} == {b}
    assert (await _identity(pw, existing)).organization_id == b
    assert only_a  # (account exists; only its organization is suspended)


@pytest.mark.asyncio
async def test_the_mcp_identity_path_is_covered_too(pw) -> None:  # noqa: F811
    a, b = await _org(pw, "mcpa"), await _org(pw, "mcpb")
    uid = await _user(pw, "mcp", a, b)
    resource = mcp_resource_identifier()

    def mcp_token(org):
        token, _issued, _expires = auth_service._issue_access_token(
            uid, org, scope=MCP_SCOPE, audience=resource, issuer=resource
        )
        return token

    token_a, token_b = mcp_token(a), mcp_token(b)
    assert (await pw.run(a, lambda s: resolve_mcp_identity(s, token_a))).organization_id == a
    await _set_status(pw, a, "suspended")

    await _expect(pw.run(a, lambda s: resolve_mcp_identity(s, token_a)), PermissionDeniedError, SUSPENDED)
    assert (await pw.run(b, lambda s: resolve_mcp_identity(s, token_b))).organization_id == b


# ============================================================ invitations


@pytest.mark.asyncio
async def test_nobody_can_start_or_finish_joining_a_suspended_organization(pw) -> None:  # noqa: F811
    org = await _org(pw, "inv")
    admin = await _user(pw, "invadmin", org)
    await pw.sup(
        "INSERT INTO user_roles (user_id, organization_id, role_id) SELECT :u, :o, id FROM roles WHERE name='admin' AND organization_id IS NULL",
        u=admin, o=org,
    )
    actor = await pw.run(org, lambda s: users_service.resolve_identity(s, admin, org))
    invite = await pw.run(
        org,
        lambda s: tenancy_service.create_invitation(s, actor, org, InvitationCreate(email=pw.email("invitee"), grants_role="viewer")),
    )
    accept = InvitationAcceptRequest(token=invite.token, password=NEW_PW, display_name="Invitee")

    # 1. starting acceptance while suspended: refused, and nothing is recorded
    await _set_status(pw, org, "suspended")
    await _expect(
        pw.run(None, lambda s: auth_service.accept_invitation_with_password(s, invite.id, accept)),
        PermissionDeniedError,
        SUSPENDED,
    )
    assert (await pw.sup("SELECT count(*) FROM email_verifications WHERE email=:e", e=pw.email("invitee")))[0][0] == 0

    # 2. acceptance was started BEFORE the suspension; the confirmation link is opened AFTER it
    await _set_status(pw, org, "active")
    delivery = await pw.run(None, lambda s: auth_service.accept_invitation_with_password(s, invite.id, accept))
    assert delivery is not None
    await _set_status(pw, org, "suspended")
    await _expect(pw.run(None, lambda s: email_verification.verify_token(s, delivery.raw_token)), PermissionDeniedError, SUSPENDED)
    assert (await pw.sup("SELECT consumed_at IS NULL FROM email_verifications WHERE id=:i", i=delivery.verification_id))[0][0] is True  # link not burned
    assert (await pw.sup("SELECT count(*) FROM users WHERE email=:e", e=pw.email("invitee")))[0][0] == 0  # no account, no role
    assert (await pw.sup("SELECT status FROM invitations WHERE id=:i", i=invite.id))[0][0] == "pending"

    # 3. once the organization is usable again the same link completes normally
    await _set_status(pw, org, "active")
    await pw.run(None, lambda s: email_verification.verify_token(s, delivery.raw_token))
    assert (await pw.sup("SELECT count(*) FROM users WHERE email=:e", e=pw.email("invitee")))[0][0] == 1


# ============================================================ organization-wide revocation


@pytest.mark.asyncio
async def test_organization_wide_revocation_ends_only_that_organizations_sessions(pw) -> None:  # noqa: F811
    a, b = await _org(pw, "ra"), await _org(pw, "rb")
    shared = await _user(pw, "shared", a, b)
    only_a = await _user(pw, "onlyra", a)
    a_tokens = [await _session(pw, shared, a), await _session(pw, shared, a), await _session(pw, only_a, a)]
    b_tokens = [await _session(pw, shared, b)]
    assert await _live_refresh_tokens(pw, a) == 3

    revoked = await pw.run(None, lambda s: auth_service.revoke_organization_sessions(s, a))

    assert revoked == 3 and await _live_refresh_tokens(pw, a) == 0
    assert await _live_refresh_tokens(pw, b) == 1  # the SAME user's session elsewhere is untouched
    for tokens in a_tokens:
        await _expect(_refresh(pw, tokens), PermissionDeniedError, "auth.refresh_token_reused")  # revoked, not merely blocked
    assert (await _refresh(pw, b_tokens[0])).access_token
    assert await pw.run(None, lambda s: auth_service.revoke_organization_sessions(s, a)) == 0  # idempotent


@pytest.mark.asyncio
async def test_revocation_is_confined_by_row_level_security_even_with_the_wrong_filter(pw) -> None:  # noqa: F811
    a, b = await _org(pw, "ia"), await _org(pw, "ib")
    uid = await _user(pw, "iso", a, b)
    await _session(pw, uid, a)
    await _session(pw, uid, b)

    # tenant context = A, statement names B: RLS hides B's rows, so nothing is revoked
    async def cross(session):
        from app.database.session import set_tenant_context
        from datetime import datetime, timezone

        await set_tenant_context(session, a)
        return await auth_repository.revoke_all_for_organization(session, b, revoked_at=datetime.now(timezone.utc))

    assert await pw.run(None, cross) == 0
    assert await _live_refresh_tokens(pw, a) == 1 and await _live_refresh_tokens(pw, b) == 1


# ============================================================ reactivation


@pytest.mark.asyncio
async def test_after_reactivation_revoked_sessions_stay_dead_and_people_sign_in_again(pw) -> None:  # noqa: F811
    org = await _org(pw, "react")
    uid = await _user(pw, "react", org)
    session = await _session(pw, uid, org)
    await _set_status(pw, org, "suspended")
    await pw.run(None, lambda s: auth_service.revoke_organization_sessions(s, org))
    await _set_status(pw, org, "active")  # reactivated

    await _expect(_refresh(pw, session), PermissionDeniedError, "auth.refresh_token_reused")  # old sessions do not come back
    fresh = await _login(pw, "react", OLD_PW)  # a fresh sign-in works
    assert (await _identity(pw, fresh)).organization_id == org


@pytest.mark.asyncio
async def test_a_pre_suspension_access_token_must_not_revive_on_reactivation(pw) -> None:  # noqa: F811
    org = await _org(pw, "revive")
    uid = await _user(pw, "revive", org)
    old = await _session(pw, uid, org)
    await _set_status(pw, org, "suspended")
    await _set_status(pw, org, "active")
    await _expect(_identity(pw, old), PermissionDeniedError, "auth.session_revoked")
