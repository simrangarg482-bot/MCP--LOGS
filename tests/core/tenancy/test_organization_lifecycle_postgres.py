"""Phase 3 of the organization lifecycle against REAL PostgreSQL: the constrained status, the
organization session epoch, the append-only history, the platform tables and the protected
SECURITY DEFINER transition function -- exercised as the real `ekip_app` role wherever the application
would run it.

Skipped unless EKIP_TEST_POSTGRES_URL / EKIP_TEST_APP_URL point at a LOCAL migrated database.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from jose import jwt as jose_jwt
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.auth import service as auth_service
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.tenancy import lifecycle
from app.core.users import service as users_service
from app.mcp.auth import resolve_mcp_identity
from app.mcp.oauth.constants import MCP_SCOPE, mcp_resource_identifier
from app.shared.config.settings import get_settings
from tests.core.users.test_role_management_postgres import _engine
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

SUSPENDED = "auth.organization_suspended"
REVOKED = "auth.session_revoked"


async def _operator(env, name, role="admin"):
    uid = await _user(env, name)
    await env.sup("INSERT INTO platform_operators (user_id, role) VALUES (:u, :r)", u=uid, r=role)
    return uid


async def _set(env, operator, org, status, reason="because", **kw):
    return await env.run(
        None,
        lambda s: lifecycle.set_organization_status(
            s, operator_user_id=operator, organization_id=org, new_status=status, reason=reason, **kw
        ),
    )


async def _row(env, org):
    return (
        await env.sup(
            "SELECT status, status_changed_at, status_message_public, session_epoch FROM organizations WHERE id=:o", o=org
        )
    )[0]


async def _app_sql(env, sql, **params):
    async def go(s):
        res = await s.execute(text(sql), params)
        return res.fetchall() if res.returns_rows else None

    return await env.run(None, go)


async def _live_refresh(env, org):
    return (await env.sup("SELECT count(*) FROM refresh_tokens WHERE organization_id=:o AND revoked_at IS NULL", o=org))[0][0]


@pytest_asyncio.fixture(autouse=True)
async def _purge_platform_audit(pw):  # noqa: F811
    """Rows these tests write to the append-only tables are removed by the OWNER with triggers off
    (the application role can never do this)."""
    migrated = await pw.sup("SELECT 1 FROM information_schema.columns WHERE table_name='organizations' AND column_name='session_epoch'")
    if not migrated:
        pytest.skip("organizations.session_epoch missing: run `alembic upgrade head` on this database")
    yield
    like = f"rm-{pw.tag}-%"
    engine = _engine(_SUPER_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL session_replication_role = replica"))
            await conn.execute(
                text(
                    "DELETE FROM platform_audit_events WHERE target_organization_id IN "
                    "(SELECT id FROM organizations WHERE slug LIKE :s) OR actor_user_id IN "
                    "(SELECT id FROM users WHERE email LIKE :e)"
                ),
                {"s": like, "e": like},
            )
            await conn.execute(
                text(
                    "DELETE FROM organization_status_history WHERE organization_id IN "
                    "(SELECT id FROM organizations WHERE slug LIKE :s)"
                ),
                {"s": like},
            )
    finally:
        await engine.dispose()


# ============================================================ status column

@pytest.mark.asyncio
async def test_status_is_not_null_defaults_to_onboarding_and_is_check_constrained(pw) -> None:  # noqa: F811
    slug = f"rm-{pw.tag}-dflt"
    await pw.sup("INSERT INTO organizations (name, slug) VALUES ('d', :s)", s=slug)
    assert (await pw.sup("SELECT status, status_changed_at, status_message_public, session_epoch FROM organizations WHERE slug=:s", s=slug))[0] == ("onboarding", None, None, 0)
    with pytest.raises(DBAPIError):
        await pw.sup("INSERT INTO organizations (name, slug, status) VALUES ('d', :s, NULL)", s=f"{slug}2")
    for bad in ("trial", "Active", "SUSPENDED", " active", "", "deleted"):
        with pytest.raises(DBAPIError):
            await pw.sup("INSERT INTO organizations (name, slug, status) VALUES ('d', :s, :st)", s=f"{slug}x", st=bad)


@pytest.mark.asyncio
async def test_the_check_constraint_is_validated(pw) -> None:  # noqa: F811
    row = await pw.sup("SELECT convalidated FROM pg_constraint WHERE conname='ck_organizations_status'")
    assert row == [(True,)]


@pytest.mark.asyncio
async def test_the_allowed_transitions_work_and_nothing_else_does(pw) -> None:  # noqa: F811
    allowed = [("onboarding", "active"), ("onboarding", "suspended"), ("active", "suspended"), ("suspended", "active")]
    for old, new in allowed:
        org = await _org(pw, f"t-{old[:3]}-{new[:3]}")
        if old != "onboarding":
            await pw.sup("UPDATE organizations SET status='active' WHERE id=:o", o=org)
            if old == "suspended":
                await pw.sup("UPDATE organizations SET status='suspended' WHERE id=:o", o=org)
        await pw.sup("UPDATE organizations SET status=:n WHERE id=:o", n=new, o=org)
        assert (await _row(pw, org))[0] == new

    forbidden = [("active", "onboarding"), ("suspended", "onboarding"), ("onboarding", "trial"), ("active", "trial")]
    for old, new in forbidden:
        org = await _org(pw, f"f-{old[:3]}-{new[:3]}")
        if old != "onboarding":
            await pw.sup("UPDATE organizations SET status='active' WHERE id=:o", o=org)
            if old == "suspended":
                await pw.sup("UPDATE organizations SET status='suspended' WHERE id=:o", o=org)
        with pytest.raises(DBAPIError):
            await pw.sup("UPDATE organizations SET status=:n WHERE id=:o", n=new, o=org)
        assert (await _row(pw, org))[0] == old


# ============================================================ database privileges

@pytest.mark.asyncio
async def test_the_application_role_cannot_change_status_or_any_lifecycle_column(pw) -> None:  # noqa: F811
    org = await _org(pw, "priv")
    for column, value in (
        ("status", "'suspended'"),
        ("session_epoch", "5"),
        ("status_changed_at", "now()"),
        ("status_message_public", "'x'"),
    ):
        with pytest.raises(DBAPIError) as info:
            await _app_sql(pw, f"UPDATE organizations SET {column}={value} WHERE id=:o", o=org)
        assert "permission denied" in str(info.value).lower(), column
    assert (await _row(pw, org))[0] == "onboarding"
    # ... while the columns the product legitimately edits still work
    await _app_sql(pw, "UPDATE organizations SET name='renamed', updated_at=now() WHERE id=:o", o=org)
    # ... and creating an organization (INSERT) and reading are unchanged
    assert (await _app_sql(pw, "SELECT status FROM organizations WHERE id=:o", o=org)) == [("onboarding",)]


@pytest.mark.asyncio
async def test_the_application_role_cannot_even_change_status_of_its_own_organization_in_context(pw) -> None:  # noqa: F811
    org = await _org(pw, "own")

    async def go(s):
        await s.execute(text("UPDATE organizations SET status='suspended' WHERE id=:o"), {"o": org})

    with pytest.raises(DBAPIError):
        await pw.run(org, go)


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["platform_operators", "organization_status_history", "platform_audit_events"])
async def test_the_application_role_has_no_access_to_the_platform_tables(pw, table) -> None:  # noqa: F811
    for sql in (
        f"SELECT * FROM {table}",
        f"DELETE FROM {table}",
        f"UPDATE {table} SET request_id='x'" if table != "platform_operators" else f"UPDATE {table} SET role='admin'",
    ):
        with pytest.raises(DBAPIError) as info:
            await _app_sql(pw, sql)
        assert "permission denied" in str(info.value).lower(), sql
    privileges = await pw.sup(
        "SELECT privilege_type FROM information_schema.role_table_grants WHERE table_name=:t AND grantee='ekip_app'", t=table
    )
    assert privileges == []


@pytest.mark.asyncio
async def test_the_application_role_cannot_insert_an_operator_or_history_row(pw) -> None:  # noqa: F811
    uid = await _user(pw, "sneak")
    with pytest.raises(DBAPIError):
        await _app_sql(pw, "INSERT INTO platform_operators (user_id, role) VALUES (:u, 'admin')", u=uid)
    org = await _org(pw, "sneakorg")
    with pytest.raises(DBAPIError):
        await _app_sql(
            pw,
            "INSERT INTO organization_status_history (organization_id, previous_status, new_status, reason, operator_user_id) "
            "VALUES (:o, 'onboarding', 'active', 'x', :u)",
            o=org,
            u=uid,
        )
    with pytest.raises(DBAPIError):
        await _app_sql(pw, "INSERT INTO platform_audit_events (actor_user_id, action) VALUES (:u, 'x')", u=uid)


@pytest.mark.asyncio
async def test_a_tenancy_manage_holder_gets_nothing_from_that_permission(pw) -> None:  # noqa: F811
    org = await _org(pw, "tm")
    admin = await _user(pw, "tmadmin", org)
    await pw.sup(
        "INSERT INTO user_roles (user_id, organization_id, role_id) SELECT :u, :o, id FROM roles WHERE name='admin' AND organization_id IS NULL",
        u=admin, o=org,
    )
    identity = await pw.run(org, lambda s: users_service.resolve_identity(s, admin, org))
    assert "tenancy:manage" in identity.permissions
    assert await pw.run(None, lambda s: lifecycle.is_active_platform_operator(s, admin)) is False
    await _expect(_set(pw, admin, org, "suspended"), PermissionDeniedError, "platform.operator_required")
    assert (await _row(pw, org))[0] == "onboarding"


@pytest.mark.asyncio
async def test_function_ownership_privileges_and_search_path(pw) -> None:  # noqa: F811
    rows = await pw.sup(
        """
        SELECT p.proname, pg_get_userbyid(p.proowner), p.prosecdef, p.proconfig::text,
               has_function_privilege('ekip_app', p.oid, 'EXECUTE'),
               has_function_privilege('public', p.oid, 'EXECUTE')
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname='public'
           AND p.proname IN ('platform_set_organization_status','platform_is_active_operator',
                             'organizations_status_lifecycle','platform_append_only')
        """
    )
    info = {r[0]: r for r in rows}
    assert set(info) == {"platform_set_organization_status", "platform_is_active_operator", "organizations_status_lifecycle", "platform_append_only"}
    superuser = (await pw.sup("SELECT current_user"))[0][0]
    for name, (_n, owner, secdef, config, app_exec, public_exec) in info.items():
        assert owner == superuser, name
        assert public_exec is False, name
        if name != "platform_append_only":
            assert secdef is True and "search_path=pg_catalog, pg_temp" in config, name
    assert info["platform_set_organization_status"][4] is True
    assert info["platform_is_active_operator"][4] is True
    assert info["organizations_status_lifecycle"][4] is False
    assert info["platform_append_only"][4] is False


@pytest.mark.asyncio
async def test_a_hostile_search_path_cannot_redirect_the_function(pw) -> None:  # noqa: F811
    org = await _org(pw, "sp")
    op = await _operator(pw, "spop")

    async def go(s):
        await s.execute(text("CREATE SCHEMA IF NOT EXISTS evil_sp"))
        await s.execute(text("CREATE TABLE evil_sp.organizations (id uuid, status text, session_epoch int)"))
        await s.execute(text("SET LOCAL search_path = evil_sp, public"))
        return await lifecycle.set_organization_status(
            s, operator_user_id=op, organization_id=org, new_status="suspended", reason="r"
        )

    try:
        change = await pw.run(None, go)
    except Exception:  # the app role may not be allowed to create schemas at all -- equally safe
        change = None
    if change is not None:
        assert (await _row(pw, org))[0] == "suspended"
    await pw.sup("DROP SCHEMA IF EXISTS evil_sp CASCADE")


# ============================================================ the transition function

@pytest.mark.asyncio
async def test_suspend_then_reactivate_writes_everything_atomically(pw) -> None:  # noqa: F811
    org = await _org(pw, "full")
    uid = await _user(pw, "full", org)
    op = await _operator(pw, "fullop")
    await _session(pw, uid, org)
    assert await _live_refresh(pw, org) == 1

    change = await _set(pw, op, org, "suspended", reason="non-payment", customer_message="Please contact billing", internal_note="CFO unreachable")
    assert (change.previous_status, change.new_status, change.session_epoch) == ("onboarding", "suspended", 1)
    status, changed_at, message, epoch = await _row(pw, org)
    assert (status, message, epoch) == ("suspended", "Please contact billing", 1) and changed_at is not None
    assert await _live_refresh(pw, org) == 0  # sessions ended in the same transaction

    hist = await pw.sup("SELECT previous_status, new_status, reason, customer_message, internal_note, operator_user_id, id FROM organization_status_history WHERE organization_id=:o", o=org)
    assert hist == [("onboarding", "suspended", "non-payment", "Please contact billing", "CFO unreachable", op, change.history_id)]
    audit = await pw.sup("SELECT actor_user_id, action, details->>'to', details->>'history_id' FROM platform_audit_events WHERE target_organization_id=:o", o=org)
    assert audit == [(op, "organization.status_changed", "suspended", str(change.history_id))]
    tenant_audit = await pw.sup("SELECT actor, action, metadata FROM audit_logs WHERE organization_id=:o AND action LIKE 'organization.%'", o=org)
    assert len(tenant_audit) == 1 and tenant_audit[0][0] == "platform" and tenant_audit[0][1] == "organization.suspended"
    assert "internal" not in str(tenant_audit[0][2]).lower() and "CFO" not in str(tenant_audit[0][2])

    back = await _set(pw, op, org, "active", reason="paid", internal_note="wire received")
    assert (back.previous_status, back.new_status, back.session_epoch) == ("suspended", "active", 2)
    status, _c, message, epoch = await _row(pw, org)
    assert (status, message, epoch) == ("active", None, 2)  # the customer-facing text does not outlive the suspension
    assert (await pw.sup("SELECT count(*) FROM organization_status_history WHERE organization_id=:o", o=org))[0][0] == 2
    assert (await pw.sup("SELECT count(*) FROM audit_logs WHERE organization_id=:o AND action='organization.reactivated'", o=org))[0][0] == 1


@pytest.mark.asyncio
async def test_onboarding_to_active_does_not_touch_sessions(pw) -> None:  # noqa: F811
    org = await _org(pw, "act")
    uid = await _user(pw, "act", org)
    op = await _operator(pw, "actop")
    live = await _session(pw, uid, org)
    change = await _set(pw, op, org, "active", reason="verified")
    assert change.session_epoch == 0 and await _live_refresh(pw, org) == 1
    assert (await _identity(pw, live)).user_id == uid  # an activation does not end anyone's session
    assert (await pw.sup("SELECT action FROM audit_logs WHERE organization_id=:o AND action LIKE 'organization.%'", o=org)) == [("organization.activated",)]


@pytest.mark.asyncio
async def test_rejections_are_explicit_and_change_nothing(pw) -> None:  # noqa: F811
    org = await _org(pw, "rej")
    op = await _operator(pw, "rejop")
    await _set(pw, op, org, "suspended", reason="r")

    await _expect(_set(pw, op, org, "suspended", reason="again"), ConflictError, "organization.already_in_status")
    await _expect(_set(pw, op, org, "onboarding", reason="back"), ConflictError, "organization.invalid_transition")
    await _expect(_set(pw, op, org, "trial", reason="x"), ConflictError, "organization.invalid_transition")
    await _expect(_set(pw, op, org, "active", reason="   "), ValidationError, "organization.invalid_status_request")
    await _expect(_set(pw, op, org, "active", reason="x" * 1001), ValidationError, "organization.invalid_status_request")
    await _expect(_set(pw, op, org, "active", reason="ok", internal_note="n" * 4001), ValidationError, "organization.invalid_status_request")
    await _expect(_set(pw, op, uuid.uuid4(), "active", reason="ok"), NotFoundError, "organization.not_found")

    await _set(pw, op, org, "active", reason="ok")
    await _expect(_set(pw, op, org, "active", reason="repeat"), ConflictError, "organization.already_in_status")
    assert (await pw.sup("SELECT count(*) FROM organization_status_history WHERE organization_id=:o", o=org))[0][0] == 2
    assert (await _row(pw, org))[3] == 2


@pytest.mark.asyncio
async def test_only_an_active_admin_operator_may_call_it(pw) -> None:  # noqa: F811
    org = await _org(pw, "who")
    stranger = await _user(pw, "stranger")
    viewer = await _operator(pw, "viewerop", role="viewer")
    revoked = await _operator(pw, "revokedop")
    await pw.sup("UPDATE platform_operators SET revoked_at=now() WHERE user_id=:u", u=revoked)
    disabled = await _operator(pw, "disabledop")
    await pw.sup("UPDATE users SET is_active=false WHERE id=:u", u=disabled)

    for caller in (stranger, viewer, revoked, disabled, uuid.uuid4()):
        await _expect(_set(pw, caller, org, "suspended"), PermissionDeniedError, "platform.operator_required")
    assert (await _row(pw, org))[0] == "onboarding"
    assert (await pw.sup("SELECT count(*) FROM organization_status_history WHERE organization_id=:o", o=org))[0][0] == 0
    # is_active_platform_operator agrees (viewer is an operator, just not an admin)
    assert await pw.run(None, lambda s: lifecycle.is_active_platform_operator(s, viewer)) is True
    for caller in (stranger, revoked, disabled):
        assert await pw.run(None, lambda s, c=caller: lifecycle.is_active_platform_operator(s, c)) is False


@pytest.mark.asyncio
async def test_a_failure_anywhere_rolls_the_whole_transition_back(pw) -> None:  # noqa: F811
    org = await _org(pw, "atom")
    uid = await _user(pw, "atom", org)
    op = await _operator(pw, "atomop")
    await _session(pw, uid, org)

    await pw.sup(
        "CREATE FUNCTION public.zz_fail_history() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected'; END $$"
    )
    await pw.sup("CREATE TRIGGER zz_fail BEFORE INSERT ON organization_status_history FOR EACH ROW EXECUTE FUNCTION public.zz_fail_history()")
    try:
        with pytest.raises(DBAPIError):
            await _set(pw, op, org, "suspended", reason="r")
    finally:
        await pw.sup("DROP TRIGGER zz_fail ON organization_status_history")
        await pw.sup("DROP FUNCTION public.zz_fail_history()")

    assert (await _row(pw, org))[0] == "onboarding" and (await _row(pw, org))[3] == 0  # status and epoch untouched
    assert await _live_refresh(pw, org) == 1  # refresh tokens not revoked
    assert (await pw.sup("SELECT count(*) FROM platform_audit_events WHERE target_organization_id=:o", o=org))[0][0] == 0
    assert (await pw.sup("SELECT count(*) FROM audit_logs WHERE organization_id=:o AND action LIKE 'organization.%'", o=org))[0][0] == 0


@pytest.mark.asyncio
async def test_concurrent_transitions_are_serialized_exactly_one_wins(pw) -> None:  # noqa: F811
    org = await _org(pw, "race")
    op = await _operator(pw, "raceop")
    results = await asyncio.gather(
        *[_set(pw, op, org, "suspended", reason=f"r{i}") for i in range(4)], return_exceptions=True
    )
    wins = [r for r in results if not isinstance(r, BaseException)]
    losses = [r for r in results if isinstance(r, ConflictError)]
    assert len(wins) == 1 and len(losses) == 3
    assert all(e.error_code == "organization.already_in_status" for e in losses)
    assert (await pw.sup("SELECT count(*) FROM organization_status_history WHERE organization_id=:o", o=org))[0][0] == 1
    assert (await _row(pw, org))[3] == 1  # the epoch was bumped once, not four times


@pytest.mark.asyncio
async def test_a_raw_superuser_status_change_still_obeys_the_rules(pw) -> None:  # noqa: F811
    """The trigger makes the guarantees hold for every writer, even a manual UPDATE."""
    org = await _org(pw, "raw")
    uid = await _user(pw, "raw", org)
    await _session(pw, uid, org)
    await pw.sup("UPDATE organizations SET status='suspended' WHERE id=:o", o=org)
    assert (await _row(pw, org))[3] == 1 and await _live_refresh(pw, org) == 0
    assert (await _row(pw, org))[1] is not None
    await pw.sup("UPDATE organizations SET status='active' WHERE id=:o", o=org)
    assert (await _row(pw, org))[3] == 2
    with pytest.raises(DBAPIError):
        await pw.sup("UPDATE organizations SET status='onboarding' WHERE id=:o", o=org)


# ============================================================ history is private and append-only

@pytest.mark.asyncio
async def test_history_and_platform_audit_are_append_only_even_for_the_owner(pw) -> None:  # noqa: F811
    org = await _org(pw, "ao")
    op = await _operator(pw, "aoop")
    await _set(pw, op, org, "suspended", reason="r", internal_note="private")
    for sql in (
        "UPDATE organization_status_history SET reason='edited' WHERE organization_id=:o",
        "DELETE FROM organization_status_history WHERE organization_id=:o",
        "UPDATE platform_audit_events SET action='edited' WHERE target_organization_id=:o",
        "DELETE FROM platform_audit_events WHERE target_organization_id=:o",
    ):
        with pytest.raises(DBAPIError) as info:
            await pw.sup(sql, o=org)
        assert "append-only" in str(info.value), sql
    assert (await pw.sup("SELECT internal_note FROM organization_status_history WHERE organization_id=:o", o=org)) == [("private",)]


@pytest.mark.asyncio
async def test_deleting_an_organization_still_removes_its_history(pw) -> None:  # noqa: F811
    org = await _org(pw, "erase")
    op = await _operator(pw, "eraseop")
    await _set(pw, op, org, "suspended", reason="r")
    await pw.sup("DELETE FROM audit_logs WHERE organization_id=:o", o=org)
    await pw.sup("DELETE FROM organizations WHERE id=:o", o=org)  # the cascade is allowed through the append-only guard
    assert (await pw.sup("SELECT count(*) FROM organization_status_history WHERE organization_id=:o", o=org))[0][0] == 0
    # the private platform audit trail intentionally outlives the organization
    assert (await pw.sup("SELECT count(*) FROM platform_audit_events WHERE target_organization_id=:o", o=org))[0][0] == 1


@pytest.mark.asyncio
async def test_internal_notes_never_reach_a_tenant_visible_surface(pw) -> None:  # noqa: F811
    org = await _org(pw, "leak")
    op = await _operator(pw, "leakop")
    await _set(pw, op, org, "suspended", reason="REASON-SECRET", customer_message="visible text", internal_note="NOTE-SECRET")

    async def everything_the_tenant_can_read(s):
        out = []
        for table in ("organizations", "audit_logs"):
            res = await s.execute(text(f"SELECT t::text FROM {table} t WHERE " + ("id=:o" if table == "organizations" else "organization_id=:o")), {"o": org})
            out += [r[0] for r in res.fetchall()]
        return " ".join(out)

    blob = await pw.run(org, everything_the_tenant_can_read)
    assert "visible text" in blob
    assert "NOTE-SECRET" not in blob and "REASON-SECRET" not in blob


# ============================================================ platform operators table

@pytest.mark.asyncio
async def test_platform_operators_constraints(pw) -> None:  # noqa: F811
    uid = await _user(pw, "opc")
    with pytest.raises(DBAPIError):
        await pw.sup("INSERT INTO platform_operators (user_id, role) VALUES (:u, 'superuser')", u=uid)
    await pw.sup("INSERT INTO platform_operators (user_id, role) VALUES (:u, 'admin')", u=uid)
    with pytest.raises(DBAPIError):  # one row per user
        await pw.sup("INSERT INTO platform_operators (user_id, role) VALUES (:u, 'viewer')", u=uid)
    row = (await pw.sup("SELECT granted_by, granted_at, revoked_at FROM platform_operators WHERE user_id=:u", u=uid))[0]
    assert row[0] is None and row[1] is not None and row[2] is None


# ============================================================ session epoch (the reactivation fix)

def _legacy_token(uid, org) -> str:
    """An access token exactly as issued BEFORE the `oe` claim existed (no `oe`)."""
    settings = get_settings()
    token, _i, _e = auth_service._issue_access_token(uid, org)
    claims = jose_jwt.get_unverified_claims(token)
    claims.pop("oe", None)
    return jose_jwt.encode(claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


class _T:  # minimal stand-in for SessionTokens
    def __init__(self, access_token):
        self.access_token = access_token


@pytest.mark.asyncio
async def test_a_pre_suspension_token_is_refused_while_suspended_and_stays_dead_after_reactivation(pw) -> None:  # noqa: F811
    org = await _org(pw, "ep1")
    uid = await _user(pw, "ep1", org)
    op = await _operator(pw, "ep1op")
    old = await _session(pw, uid, org)
    assert auth_service.verify_access_token(old.access_token).org_session_epoch == 0
    assert (await _identity(pw, old)).user_id == uid

    await _set(pw, op, org, "suspended", reason="r")
    await _expect(_identity(pw, old), PermissionDeniedError, SUSPENDED)
    await _set(pw, op, org, "active", reason="r")
    await _expect(_identity(pw, old), PermissionDeniedError, REVOKED)  # NOT revived
    await _expect(_identity(pw, old), PermissionDeniedError, REVOKED)  # ... and never will be


@pytest.mark.asyncio
async def test_a_token_issued_after_reactivation_works_and_users_sign_in_again(pw) -> None:  # noqa: F811
    org = await _org(pw, "ep2")
    uid = await _user(pw, "ep2", org)
    op = await _operator(pw, "ep2op")
    old = await _session(pw, uid, org)
    await _set(pw, op, org, "suspended", reason="r")
    await _set(pw, op, org, "active", reason="r")

    fresh = await _login(pw, "ep2", OLD_PW)  # re-authentication
    assert auth_service.verify_access_token(fresh.access_token).org_session_epoch == 2
    assert (await _identity(pw, fresh)).organization_id == org
    # the pre-suspension refresh token was revoked at suspension: it cannot mint a session either
    await _expect(
        pw.run(None, lambda s: auth_service.refresh(s, __import__("app.core.auth.schemas", fromlist=["RefreshRequest"]).RefreshRequest(refresh_token=old.refresh_token))),
        PermissionDeniedError,
        "auth.refresh_token_reused",
    )


@pytest.mark.asyncio
async def test_the_same_users_other_organization_is_unaffected_by_suspend_and_reactivate(pw) -> None:  # noqa: F811
    a, b = await _org(pw, "ep3a"), await _org(pw, "ep3b")
    uid = await _user(pw, "ep3", a, b)
    op = await _operator(pw, "ep3op")
    in_a, in_b = await _session(pw, uid, a), await _session(pw, uid, b)
    await _set(pw, op, a, "suspended", reason="r")
    await _set(pw, op, a, "active", reason="r")
    assert (await _identity(pw, in_b)).organization_id == b  # token for B still valid
    assert await _live_refresh(pw, b) == 1  # and B's refresh token was not revoked
    assert (await _row(pw, b))[3] == 0
    await _expect(_identity(pw, in_a), PermissionDeniedError, REVOKED)


@pytest.mark.asyncio
async def test_a_legacy_token_without_the_claim_counts_as_epoch_zero(pw) -> None:  # noqa: F811
    org = await _org(pw, "ep4")
    uid = await _user(pw, "ep4", org)
    op = await _operator(pw, "ep4op")
    legacy = _T(_legacy_token(uid, org))
    assert auth_service.verify_access_token(legacy.access_token).org_session_epoch == 0
    assert (await _identity(pw, legacy)).user_id == uid  # works while the organization has never been suspended
    await _set(pw, op, org, "active", reason="r")  # onboarding -> active does not move the epoch
    assert (await _identity(pw, legacy)).user_id == uid
    await _set(pw, op, org, "suspended", reason="r")
    await _expect(_identity(pw, legacy), PermissionDeniedError, SUSPENDED)
    await _set(pw, op, org, "active", reason="r")
    await _expect(_identity(pw, legacy), PermissionDeniedError, REVOKED)  # absent claim cannot revive either


@pytest.mark.asyncio
async def test_an_access_token_cannot_claim_a_different_epoch_to_get_in(pw) -> None:  # noqa: F811
    org = await _org(pw, "ep5")
    uid = await _user(pw, "ep5", org)
    op = await _operator(pw, "ep5op")
    await _set(pw, op, org, "suspended", reason="r")
    await _set(pw, op, org, "active", reason="r")  # epoch is now 2

    def forged(oe):
        token, _i, _e = auth_service._issue_access_token(uid, org, org_session_epoch=oe)
        return _T(token)

    assert (await _identity(pw, forged(2))).user_id == uid  # the legitimate value works (needs the signing key anyway)
    for stale in (0, 1, 3):
        await _expect(_identity(pw, forged(stale)), PermissionDeniedError, REVOKED)


@pytest.mark.asyncio
async def test_rest_and_mcp_behave_identically_for_the_epoch(pw) -> None:  # noqa: F811
    org = await _org(pw, "ep6")
    uid = await _user(pw, "ep6", org)
    op = await _operator(pw, "ep6op")
    resource = mcp_resource_identifier()

    def mcp_token(epoch):
        token, _i, _e = auth_service._issue_access_token(
            uid, org, scope=MCP_SCOPE, audience=resource, issuer=resource, org_session_epoch=epoch
        )
        return token

    old_mcp = mcp_token(0)
    old_rest = await _session(pw, uid, org)
    assert (await pw.run(org, lambda s: resolve_mcp_identity(s, old_mcp))).organization_id == org
    await _set(pw, op, org, "suspended", reason="r")
    await _expect(pw.run(org, lambda s: resolve_mcp_identity(s, old_mcp)), PermissionDeniedError, SUSPENDED)
    await _set(pw, op, org, "active", reason="r")
    await _expect(pw.run(org, lambda s: resolve_mcp_identity(s, old_mcp)), PermissionDeniedError, REVOKED)
    await _expect(_identity(pw, old_rest), PermissionDeniedError, REVOKED)
    new_mcp = mcp_token(2)
    assert (await pw.run(org, lambda s: resolve_mcp_identity(s, new_mcp))).organization_id == org


@pytest.mark.asyncio
async def test_an_epoch_check_needs_no_clock_comparison(pw) -> None:  # noqa: F811
    """A token whose timestamps are in the far future (a skewed API-server clock) is judged by the
    epoch alone -- the rule never compares the database clock with the token's."""
    org = await _org(pw, "ep7")
    uid = await _user(pw, "ep7", org)
    op = await _operator(pw, "ep7op")
    old = await _session(pw, uid, org)
    await _set(pw, op, org, "suspended", reason="r")
    await _set(pw, op, org, "active", reason="r")
    settings = get_settings()
    claims = jose_jwt.get_unverified_claims(old.access_token)
    claims["iat_us"] = claims["iat_us"] + 3_600_000_000  # an hour "in the future"
    claims["exp"] = claims["exp"] + 7200
    skewed = _T(jose_jwt.encode(claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm))
    await _expect(_identity(pw, skewed), PermissionDeniedError, REVOKED)


@pytest.mark.asyncio
async def test_a_suspended_organization_issues_no_new_session_and_a_reactivated_one_does(pw) -> None:  # noqa: F811
    org = await _org(pw, "ep8")
    uid = await _user(pw, "ep8", org)
    op = await _operator(pw, "ep8op")
    await _set(pw, op, org, "suspended", reason="r")
    await _expect(_login(pw, "ep8", OLD_PW), PermissionDeniedError, SUSPENDED)
    await _set(pw, op, org, "active", reason="r")
    assert (await _identity(pw, await _login(pw, "ep8", OLD_PW))).user_id == uid
