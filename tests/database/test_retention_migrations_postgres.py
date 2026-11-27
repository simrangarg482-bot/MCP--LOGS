"""Migrations `d9b2f4a7c1e3` (retention support) and `e1c7a9d3b5f2` (storage monitoring + audit-archive ledger) on real
Postgres in a throw-away database on a LOCAL server: additive, `audit_logs` untouched, FORCE RLS on the tenant
tables, narrow definer functions, and clean downgrades that keep data."""

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

_BASE = "c1a5e7f3b9d2"
_RETENTION = "d9b2f4a7c1e3"
_STORAGE = "e1c7a9d3b5f2"
_NEW_TABLES = ("storage_snapshots", "storage_snapshot_tables", "storage_snapshot_tenants", "audit_archive_batches")
_TENANT_TABLES = ("storage_snapshot_tenants", "audit_archive_batches")


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


def test_retention_migration_is_additive_and_the_batched_purge_is_narrow(database) -> None:
    assert _alembic(database, "upgrade", _BASE).returncode == 0
    asyncio.run(_exec(database, "INSERT INTO organizations (name, slug, status) VALUES ('A','a','active')"))
    asyncio.run(_exec(database, "INSERT INTO mcp_requests (tool_name, identity, occurred_at) VALUES ('t','u', now() - interval '200 days')"))
    columns_before = {r["column_name"] for r in asyncio.run(_fetch(database, "SELECT column_name FROM information_schema.columns WHERE table_name='documents'"))}

    result = _alembic(database, "upgrade", _RETENTION)
    assert result.returncode == 0, result.stderr[-2000:]

    async def check():
        columns = {r["column_name"] for r in await _fetch(database, "SELECT column_name FROM information_schema.columns WHERE table_name='documents'")}
        assert columns - columns_before == {"tombstoned_at"}  # exactly one new, nullable column
        nullable = (await _fetch(database, "SELECT is_nullable FROM information_schema.columns WHERE table_name='documents' AND column_name='tombstoned_at'"))[0]["is_nullable"]
        assert nullable == "YES"
        fn = (
            await _fetch(
                database,
                "SELECT p.prosecdef AS secdef, p.proconfig::text AS cfg, has_function_privilege('ekip_app', p.oid, 'EXECUTE') AS app, "
                "has_function_privilege('public', p.oid, 'EXECUTE') AS pub FROM pg_proc p WHERE p.proname='purge_mcp_requests_batch'",
            )
        )[0]
        assert fn["secdef"] is True and "search_path=pg_catalog, public, pg_temp" in fn["cfg"] and fn["app"] is True and fn["pub"] is False
        assert (await _fetch(database, "SELECT 1 FROM pg_proc WHERE proname='purge_mcp_requests'"))  # the original is still there
        assert (await _fetch(database, "SELECT count(*) AS n FROM mcp_requests"))[0]["n"] == 1  # nothing was deleted by migrating
        # the function deletes in batches and returns how many
        assert (await _fetch(database, "SELECT public.purge_mcp_requests_batch(now() - interval '90 days', 1) AS n"))[0]["n"] == 1
        assert (await _fetch(database, "SELECT public.purge_mcp_requests_batch(now() - interval '90 days', 1) AS n"))[0]["n"] == 0

    asyncio.run(check())


def test_storage_migration_adds_tables_rls_and_leaves_audit_logs_alone(database) -> None:
    before = {r["table_name"] for r in asyncio.run(_fetch(database, "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))}
    audit_cols = {r["column_name"] for r in asyncio.run(_fetch(database, "SELECT column_name FROM information_schema.columns WHERE table_name='audit_logs'"))}
    audit_idx = {r["indexname"] for r in asyncio.run(_fetch(database, "SELECT indexname FROM pg_indexes WHERE tablename='audit_logs'"))}
    result = _alembic(database, "upgrade", _STORAGE)
    assert result.returncode == 0, result.stderr[-2000:]

    async def check():
        after = {r["table_name"] for r in await _fetch(database, "SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
        assert after - before == set(_NEW_TABLES)
        for table in _TENANT_TABLES:
            rls = (await _fetch(database, "SELECT relrowsecurity AS r, relforcerowsecurity AS f FROM pg_class WHERE relname=$1", table))[0]
            assert rls["r"] is True and rls["f"] is True, table
            assert [p["policyname"] for p in await _fetch(database, "SELECT policyname FROM pg_policies WHERE tablename=$1", table)] == ["tenant_isolation"]
        for table in ("storage_snapshots", "storage_snapshot_tables"):  # database-level facts: no tenant column, no policy
            assert not await _fetch(database, "SELECT 1 FROM pg_policies WHERE tablename=$1", table)
        # audit_logs is exactly as it was: same columns, same indexes
        assert {r["column_name"] for r in await _fetch(database, "SELECT column_name FROM information_schema.columns WHERE table_name='audit_logs'")} == audit_cols
        assert {r["indexname"] for r in await _fetch(database, "SELECT indexname FROM pg_indexes WHERE tablename='audit_logs'")} == audit_idx
        fn = (
            await _fetch(
                database,
                "SELECT p.prosecdef AS secdef, p.proconfig::text AS cfg, has_function_privilege('ekip_app', p.oid, 'EXECUTE') AS app, "
                "has_function_privilege('public', p.oid, 'EXECUTE') AS pub FROM pg_proc p WHERE p.proname='platform_storage_tenants'",
            )
        )[0]
        assert fn["secdef"] is True and "search_path=pg_catalog, pg_temp" in fn["cfg"] and fn["app"] is True and fn["pub"] is False
        constraint = (await _fetch(database, "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint WHERE conname='ck_audit_archive_status'"))[0]["d"]
        assert all(word in constraint for word in ("exported", "verified", "failed"))

    asyncio.run(check())


def test_downgrades_remove_only_what_they_added_and_a_reupgrade_is_clean(database) -> None:
    asyncio.run(_exec(database, "INSERT INTO audit_logs (organization_id, actor, action, resource_type) SELECT id,'user:x','a.b','t' FROM organizations LIMIT 1"))
    down = _alembic(database, "downgrade", _RETENTION)
    assert down.returncode == 0, down.stderr[-2000:]

    async def after_storage_down():
        tables = {r["table_name"] for r in await _fetch(database, "SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
        assert not tables & set(_NEW_TABLES)
        assert not await _fetch(database, "SELECT 1 FROM pg_proc WHERE proname='platform_storage_tenants'")
        assert (await _fetch(database, "SELECT count(*) AS n FROM audit_logs"))[0]["n"] == 1  # audit history is untouched

    asyncio.run(after_storage_down())
    down2 = _alembic(database, "downgrade", _BASE)
    assert down2.returncode == 0, down2.stderr[-2000:]

    async def after_retention_down():
        cols = {r["column_name"] for r in await _fetch(database, "SELECT column_name FROM information_schema.columns WHERE table_name='documents'")}
        assert "tombstoned_at" not in cols
        assert not await _fetch(database, "SELECT 1 FROM pg_proc WHERE proname='purge_mcp_requests_batch'")
        assert await _fetch(database, "SELECT 1 FROM pg_proc WHERE proname='purge_mcp_requests'")

    asyncio.run(after_retention_down())
    assert _alembic(database, "upgrade", _STORAGE).returncode == 0
    assert asyncio.run(_fetch(database, "SELECT count(*) AS n FROM audit_logs"))[0]["n"] == 1
