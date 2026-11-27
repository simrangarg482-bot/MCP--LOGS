"""Migration `a8e3c5d1f2b7` (organization_member_states) on real Postgres in a throw-away database on a LOCAL
server: additive (no existing membership is touched, no row is created), RLS forced, trigger and lookup
function in place with the right privileges, and a clean downgrade / re-upgrade."""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest

from tests.database.test_roles_migration_postgres import (  # noqa: F401  (helpers + skip mark)
    _alembic,
    _dsn,
    _exec,
    _fetch,
    pytestmark,
)

_PREVIOUS = "f7c1d3a9b2e5"
_REVISION = "a8e3c5d1f2b7"


@pytest.fixture(scope="module")
def database():
    name = f"ekip_mig_{uuid.uuid4().hex[:8]}"

    async def run(sql: str) -> None:
        conn = await asyncpg.connect(_dsn("postgres"))
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(run(f'CREATE DATABASE "{name}"'))
    yield name
    asyncio.run(run(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


_ROWS = """
INSERT INTO organizations (name, slug, status) VALUES ('A', 'org-a', 'active'), ('B', 'org-b', 'onboarding');
INSERT INTO users (email, display_name, is_active, email_verified_at)
    VALUES ('u@x.test', 'U', true, now()), ('v@x.test', 'V', true, now());
INSERT INTO roles (organization_id, name, description) SELECT id, 'member', 'x' FROM organizations;
INSERT INTO user_roles (user_id, organization_id, role_id)
    SELECT u.id, o.id, r.id FROM users u CROSS JOIN organizations o JOIN roles r ON r.organization_id = o.id;
INSERT INTO refresh_tokens (user_id, organization_id, family_id, token_hash, expires_at)
    SELECT u.id, o.id, gen_random_uuid(), 'rt-' || u.email || o.slug, now() + interval '30 days'
    FROM users u CROSS JOIN organizations o;
"""


def test_upgrade_is_additive_creates_no_state_and_touches_no_membership(database) -> None:
    assert _alembic(database, "upgrade", _PREVIOUS).returncode == 0
    asyncio.run(_exec(database, _ROWS))
    roles_before = asyncio.run(_fetch(database, "SELECT user_id, organization_id, role_id FROM user_roles ORDER BY 1,2,3"))
    users_before = asyncio.run(_fetch(database, "SELECT id, is_active, email_verified_at FROM users ORDER BY id"))

    result = _alembic(database, "upgrade", _REVISION)
    assert result.returncode == 0, result.stderr[-2000:]

    async def check():
        # nothing was backfilled: a missing row means active, so every membership keeps working as it was
        assert (await _fetch(database, "SELECT count(*) AS n FROM organization_member_states"))[0]["n"] == 0
        assert [tuple(r) for r in await _fetch(database, "SELECT user_id, organization_id, role_id FROM user_roles ORDER BY 1,2,3")] == [tuple(r) for r in roles_before]
        assert [tuple(r) for r in await _fetch(database, "SELECT id, is_active, email_verified_at FROM users ORDER BY id")] == [tuple(r) for r in users_before]
        assert (await _fetch(database, "SELECT count(*) AS n FROM refresh_tokens WHERE revoked_at IS NULL"))[0]["n"] == 4  # no session ended
        # row-level security, forced, with the tenant policy
        rls = (await _fetch(database, "SELECT relrowsecurity AS r, relforcerowsecurity AS f FROM pg_class WHERE relname='organization_member_states'"))[0]
        assert rls["r"] is True and rls["f"] is True
        policy = await _fetch(database, "SELECT policyname FROM pg_policies WHERE tablename='organization_member_states'")
        assert [p["policyname"] for p in policy] == ["tenant_isolation"]
        # the trigger and the narrow lookup function
        assert await _fetch(database, "SELECT 1 FROM pg_trigger WHERE tgname='organization_member_state_guard' AND NOT tgisinternal")
        fn = (
            await _fetch(
                database,
                "SELECT p.prosecdef AS secdef, p.proconfig::text AS cfg, has_function_privilege('ekip_app', p.oid, 'EXECUTE') AS app, "
                "has_function_privilege('public', p.oid, 'EXECUTE') AS pub FROM pg_proc p WHERE p.proname='member_deactivated_organization_ids'",
            )
        )[0]
        assert fn["secdef"] is True and "search_path=pg_catalog, pg_temp" in fn["cfg"] and fn["app"] is True and fn["pub"] is False
        # defaults and constraints
        cols = {c["column_name"]: c for c in await _fetch(database, "SELECT column_name, column_default, is_nullable FROM information_schema.columns WHERE table_name='organization_member_states'")}
        assert "'active'" in cols["status"]["column_default"] and cols["status"]["is_nullable"] == "NO"
        assert cols["member_epoch"]["column_default"] == "0"
        conn = await asyncpg.connect(_dsn(database))
        try:
            org, user = (await conn.fetchrow("SELECT o.id, u.id FROM organizations o, users u LIMIT 1"))
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("INSERT INTO organization_member_states (organization_id, user_id, status) VALUES ($1,$2,'suspended')", org, user)
            await conn.execute("INSERT INTO organization_member_states (organization_id, user_id, status) VALUES ($1,$2,'deactivated')", org, user)
            assert await conn.fetchval("SELECT member_epoch FROM organization_member_states") == 1
            # deleting the organization/user cascades (only that may remove the row)
            await conn.execute("DELETE FROM user_roles WHERE user_id=$1", user)
            await conn.execute("DELETE FROM refresh_tokens WHERE user_id=$1", user)
            await conn.execute("DELETE FROM users WHERE id=$1", user)
            assert await conn.fetchval("SELECT count(*) FROM organization_member_states") == 0
        finally:
            await conn.close()

    asyncio.run(check())


def test_downgrade_removes_only_this_feature_and_a_reupgrade_is_clean(database) -> None:
    down = _alembic(database, "downgrade", _PREVIOUS)
    assert down.returncode == 0, down.stderr[-2000:]

    async def check():
        assert not await _fetch(database, "SELECT 1 FROM information_schema.tables WHERE table_name='organization_member_states'")
        assert not await _fetch(database, "SELECT 1 FROM pg_proc WHERE proname IN ('member_deactivated_organization_ids','organization_member_state_guard')")
        # everything else is untouched
        assert (await _fetch(database, "SELECT count(*) AS n FROM organizations"))[0]["n"] == 2
        assert (await _fetch(database, "SELECT count(*) AS n FROM user_roles"))[0]["n"] == 2  # one user was removed above
        assert await _fetch(database, "SELECT 1 FROM pg_proc WHERE proname='platform_list_organizations'")  # earlier migrations intact

    asyncio.run(check())
    assert _alembic(database, "upgrade", "head").returncode == 0
    assert asyncio.run(_fetch(database, "SELECT count(*) AS n FROM organization_member_states"))[0]["n"] == 0
