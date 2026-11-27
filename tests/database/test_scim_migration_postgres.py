"""Migration `b9f4d6e2a1c8` (SCIM provisioning) on real Postgres in a throw-away database on a LOCAL server:
additive, FORCE RLS on all four tables, the narrow token function, and a clean downgrade."""

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

_PREVIOUS = "a8e3c5d1f2b7"
_REVISION = "b9f4d6e2a1c8"
_TABLES = ("scim_configurations", "scim_tokens", "scim_identities", "scim_pending_actions")


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


def test_upgrade_is_additive_and_creates_no_rows(database) -> None:
    assert _alembic(database, "upgrade", _PREVIOUS).returncode == 0
    asyncio.run(_exec(database, "INSERT INTO organizations (name, slug, status) VALUES ('A','a','active')"))
    asyncio.run(_exec(database, "INSERT INTO users (email, display_name, is_active, email_verified_at) VALUES ('u@x.test','U',true,now())"))
    before = asyncio.run(_fetch(database, "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))

    result = _alembic(database, "upgrade", _REVISION)
    assert result.returncode == 0, result.stderr[-2000:]

    async def check():
        after = {r["table_name"] for r in await _fetch(database, "SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
        assert after - {r["table_name"] for r in before} == set(_TABLES)  # exactly the four new tables
        for table in _TABLES:
            assert (await _fetch(database, f"SELECT count(*) AS n FROM {table}"))[0]["n"] == 0  # nothing is created or enabled
            rls = (await _fetch(database, "SELECT relrowsecurity AS r, relforcerowsecurity AS f FROM pg_class WHERE relname=$1", table))[0]
            assert rls["r"] is True and rls["f"] is True, table
            assert [p["policyname"] for p in await _fetch(database, "SELECT policyname FROM pg_policies WHERE tablename=$1", table)] == ["tenant_isolation"]
        assert (await _fetch(database, "SELECT count(*) AS n FROM organizations"))[0]["n"] == 1
        assert (await _fetch(database, "SELECT is_active FROM users"))[0]["is_active"] is True
        # SCIM is OFF unless an organization opts in: the default of `enabled` is false
        default = (await _fetch(database, "SELECT column_default FROM information_schema.columns WHERE table_name='scim_configurations' AND column_name='enabled'"))[0]["column_default"]
        assert default == "false"
        fn = (
            await _fetch(
                database,
                "SELECT p.prosecdef AS secdef, p.proconfig::text AS cfg, has_function_privilege('ekip_app', p.oid, 'EXECUTE') AS app, "
                "has_function_privilege('public', p.oid, 'EXECUTE') AS pub FROM pg_proc p WHERE p.proname='scim_authenticate_token'",
            )
        )[0]
        assert fn["secdef"] is True and "search_path=pg_catalog, pg_temp" in fn["cfg"] and fn["app"] is True and fn["pub"] is False
        # the hash column is unique and there is no column that could hold a raw token
        cols = {r["column_name"] for r in await _fetch(database, "SELECT column_name FROM information_schema.columns WHERE table_name='scim_tokens'")}
        assert "token_hash" in cols and not cols & {"token", "raw_token", "secret"}

    asyncio.run(check())


def test_downgrade_removes_only_scim_and_a_reupgrade_is_clean(database) -> None:
    down = _alembic(database, "downgrade", _PREVIOUS)
    assert down.returncode == 0, down.stderr[-2000:]

    async def check():
        for table in _TABLES:
            assert not await _fetch(database, "SELECT 1 FROM information_schema.tables WHERE table_name=$1", table)
        assert not await _fetch(database, "SELECT 1 FROM pg_proc WHERE proname='scim_authenticate_token'")
        assert await _fetch(database, "SELECT 1 FROM information_schema.tables WHERE table_name='organization_member_states'")  # Phase 1 intact
        assert (await _fetch(database, "SELECT count(*) AS n FROM organizations"))[0]["n"] == 1

    asyncio.run(check())
    assert _alembic(database, "upgrade", "head").returncode == 0
