"""Migrations `e5b8c2d9f4a7` (operator authentication helpers) and `f7c1d3a9b2e5` (platform read functions)
on real Postgres in a throw-away database on a LOCAL server: privileges, ownership, search_path, and a clean
downgrade/upgrade round trip that never touches organization data."""

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

_LIFECYCLE = "d4a7b1c8e3f6"
_READS = "f7c1d3a9b2e5"
_FUNCTIONS = {
    "platform_operator_role": "(uuid)",
    "platform_record_login": "(uuid, text)",
    "platform_list_organizations": "(uuid, text, text, integer, integer)",
    "platform_get_organization": "(uuid, uuid)",
    "platform_organization_status_history": "(uuid, uuid, integer, integer)",
}


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


def test_upgrade_adds_only_functions_with_the_right_privileges(database) -> None:
    assert _alembic(database, "upgrade", _LIFECYCLE).returncode == 0
    asyncio.run(
        _exec(
            database,
            "INSERT INTO organizations (name, slug, status) VALUES "
            "('A','a','onboarding'),('B','b','active'),('C','c','suspended')",
        )
    )
    sql_tables = "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
    before_tables = {r["table_name"] for r in asyncio.run(_fetch(database, sql_tables))}

    result = _alembic(database, "upgrade", _READS)
    assert result.returncode == 0, result.stderr[-2000:]

    async def check():
        after_tables = {r["table_name"] for r in await _fetch(database, sql_tables)}
        assert after_tables == before_tables  # no table added or removed
        statuses = [r["status"] for r in await _fetch(database, "SELECT status FROM organizations ORDER BY slug")]
        assert statuses == ["onboarding", "active", "suspended"]
        owner = (await _fetch(database, "SELECT current_user AS u"))[0]["u"]
        for name, args in _FUNCTIONS.items():
            row = (
                await _fetch(
                    database,
                    "SELECT pg_get_userbyid(p.proowner) AS owner, p.prosecdef AS secdef, p.proconfig::text AS cfg, "
                    "has_function_privilege('ekip_app', p.oid, 'EXECUTE') AS app, "
                    "has_function_privilege('public', p.oid, 'EXECUTE') AS pub "
                    f"FROM pg_proc p WHERE p.oid = 'public.{name}{args}'::regprocedure",
                )
            )[0]
            assert row["owner"] == owner and row["secdef"] is True, name
            assert "search_path=pg_catalog, pg_temp" in row["cfg"], name
            assert row["app"] is True and row["pub"] is False, name
        for table in ("platform_operators", "organization_status_history", "platform_audit_events"):
            has = await _fetch(database, f"SELECT has_table_privilege('ekip_app','{table}','SELECT') AS p")
            assert has[0]["p"] is False

    asyncio.run(check())


def test_the_functions_refuse_non_operators_and_work_for_operators(database) -> None:
    async def check():
        conn = await asyncpg.connect(_dsn(database))
        try:
            await conn.execute(
                "INSERT INTO users (email, display_name, is_active, email_verified_at) VALUES "
                "('op@x.test','Op',true,now()), ('no@x.test','No',true,now())"
            )
            op = await conn.fetchval("SELECT id FROM users WHERE email='op@x.test'")
            nobody = await conn.fetchval("SELECT id FROM users WHERE email='no@x.test'")
            await conn.execute("INSERT INTO platform_operators (user_id, role) VALUES ($1,'viewer')", op)
            assert await conn.fetchval("SELECT platform_operator_role($1)", op) == "viewer"
            assert await conn.fetchval("SELECT platform_operator_role($1)", nobody) is None
            assert await conn.fetchval("SELECT count(*) FROM platform_list_organizations($1)", op) == 3
            assert await conn.fetchval("SELECT count(*) FROM platform_list_organizations($1, NULL, 'suspended')", op) == 1
            for sql in (
                "SELECT * FROM platform_list_organizations($1)",
                "SELECT * FROM platform_get_organization($1, gen_random_uuid())",
                "SELECT * FROM platform_organization_status_history($1, gen_random_uuid())",
                "SELECT platform_record_login($1)",
            ):
                with pytest.raises(asyncpg.PostgresError) as info:
                    await conn.fetch(sql, nobody)
                assert info.value.sqlstate == "EK003", sql
            with pytest.raises(asyncpg.PostgresError) as info:
                await conn.fetch("SELECT * FROM platform_get_organization($1, gen_random_uuid())", op)
            assert info.value.sqlstate == "EK004"
            with pytest.raises(asyncpg.PostgresError) as info:
                await conn.fetch("SELECT * FROM platform_list_organizations($1, NULL, 'bogus')", op)
            assert info.value.sqlstate == "EK005"
            # an unverified account is not an operator, whatever the grant says
            await conn.execute("UPDATE users SET email_verified_at = NULL WHERE id=$1", op)
            assert await conn.fetchval("SELECT platform_operator_role($1)", op) is None
        finally:
            await conn.close()

    asyncio.run(check())


def test_downgrade_removes_only_these_functions_and_round_trips(database) -> None:
    down = _alembic(database, "downgrade", _LIFECYCLE)
    assert down.returncode == 0, down.stderr[-2000:]
    for name in _FUNCTIONS:
        assert not asyncio.run(_fetch(database, "SELECT 1 FROM pg_proc WHERE proname=$1", name)), name
    # the Phase 3 objects are untouched
    assert asyncio.run(_fetch(database, "SELECT 1 FROM pg_proc WHERE proname='platform_set_organization_status'"))
    assert asyncio.run(_fetch(database, "SELECT count(*) AS n FROM organizations"))[0]["n"] == 3
    assert _alembic(database, "upgrade", "head").returncode == 0
    assert asyncio.run(_fetch(database, "SELECT 1 FROM pg_proc WHERE proname='platform_list_organizations'"))
