"""Migration `c1a5e7f3b9d2` (SCIM groups and role provenance) on real Postgres in a throw-away database on a LOCAL
server: additive, existing roles become `manual`, FORCE RLS on the four new tables, a clean downgrade that leaves
the role rows themselves untouched."""

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

_PREVIOUS = "b9f4d6e2a1c8"
_REVISION = "c1a5e7f3b9d2"
_TABLES = ("scim_groups", "scim_group_members", "scim_group_role_mappings", "scim_role_grants")


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


def test_upgrade_is_additive_and_every_existing_role_becomes_manual(database) -> None:
    assert _alembic(database, "upgrade", _PREVIOUS).returncode == 0
    asyncio.run(_exec(database, "INSERT INTO organizations (name, slug, status) VALUES ('A','a','active')"))
    asyncio.run(_exec(database, "INSERT INTO users (email, display_name, is_active, email_verified_at) VALUES ('u@x.test','U',true,now())"))
    asyncio.run(_exec(
        database,
        "INSERT INTO user_roles (user_id, organization_id, role_id) "
        "SELECT u.id, o.id, r.id FROM users u, organizations o, roles r WHERE r.name='admin' AND r.organization_id IS NULL",
    ))
    before = {r["table_name"] for r in asyncio.run(_fetch(database, "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))}

    result = _alembic(database, "upgrade", _REVISION)
    assert result.returncode == 0, result.stderr[-2000:]

    async def check():
        after = {r["table_name"] for r in await _fetch(database, "SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
        assert after - before == set(_TABLES)
        for table in _TABLES:
            assert (await _fetch(database, f"SELECT count(*) AS n FROM {table}"))[0]["n"] == 0  # no group, mapping or grant exists
            rls = (await _fetch(database, "SELECT relrowsecurity AS r, relforcerowsecurity AS f FROM pg_class WHERE relname=$1", table))[0]
            assert rls["r"] is True and rls["f"] is True, table
            assert [p["policyname"] for p in await _fetch(database, "SELECT policyname FROM pg_policies WHERE tablename=$1", table)] == ["tenant_isolation"]
        rows = await _fetch(database, "SELECT granted_via FROM user_roles")
        assert rows and {r["granted_via"] for r in rows} == {"manual"}  # nothing pre-existing can be removed by sync
        check = await _fetch(database, "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint WHERE conname='ck_user_roles_granted_via'")
        assert "manual" in check[0]["d"] and "scim" in check[0]["d"]
        # a group can never grant anything until an administrator maps it: the mapping table starts empty
        # and the application role cannot read another tenant's rows without a tenant context
        assert (await _fetch(database, "SELECT count(*) AS n FROM organizations"))[0]["n"] == 1

    asyncio.run(check())


def test_downgrade_removes_only_the_group_tables_and_keeps_the_roles(database) -> None:
    down = _alembic(database, "downgrade", _PREVIOUS)
    assert down.returncode == 0, down.stderr[-2000:]

    async def check():
        tables = {r["table_name"] for r in await _fetch(database, "SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
        assert not tables & set(_TABLES)
        assert "scim_identities" in tables  # Phase 2 is intact
        cols = {r["column_name"] for r in await _fetch(database, "SELECT column_name FROM information_schema.columns WHERE table_name='user_roles'")}
        assert "granted_via" not in cols
        assert (await _fetch(database, "SELECT count(*) AS n FROM user_roles"))[0]["n"] == 1  # the role row itself survived

    asyncio.run(check())
    again = _alembic(database, "upgrade", _REVISION)
    assert again.returncode == 0, again.stderr[-2000:]
    assert asyncio.run(_fetch(database, "SELECT granted_via FROM user_roles"))[0]["granted_via"] == "manual"
