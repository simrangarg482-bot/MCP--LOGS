"""HNSW migration `b7c1d9e4f2a3` against REAL PostgreSQL (pgvector >= 0.8.0).

The migration's own `upgrade()`/`downgrade()` run through Alembic's operations
and migration context on a real connection, exactly as `alembic upgrade` runs
them (including `autocommit_block()`), on a populated database.

Skipped unless EKIP_TEST_POSTGRES_URL / EKIP_TEST_APP_URL point at a LOCAL,
disposable, migrated database (see `tests/retrieval/hnsw_support.py`). The
fixture leaves the database with the indexes present, as `alembic upgrade head`
would.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import asyncpg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from app.database.session import Base
from tests.retrieval import hnsw_support as H

pytestmark = H.requires_pg

_DEFINITION = "USING hnsw (embedding vector_ip_ops) WITH (m='16', ef_construction='128')"


async def _fetch(sql: str, *args):
    conn = await asyncpg.connect(H.asyncpg_dsn(H.SUPER_URL), ssl=False)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def _indexes() -> dict[str, tuple[bool, str, int]]:
    rows = await _fetch(
        "SELECT c.relname, i.indisvalid, pg_get_indexdef(i.indexrelid) AS d, c.oid::int AS oid "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname LIKE 'ix\\_%\\_embedding\\_hnsw'"
    )
    return {r["relname"]: (r["indisvalid"], r["d"], r["oid"]) for r in rows}


async def _fingerprint() -> dict:
    """Row counts + an id hash per table, and a hash of every RLS policy."""
    out = {}
    for table in H.TABLES:
        rows = await _fetch(f"SELECT id FROM {table} ORDER BY id")
        out[table] = (
            len(rows),
            hashlib.md5("".join(str(r["id"]) for r in rows).encode()).hexdigest(),
        )
    policies = await _fetch(
        "SELECT tablename, policyname, cmd, coalesce(qual,'') q, coalesce(with_check,'') w "
        "FROM pg_policies ORDER BY tablename, policyname"
    )
    out["policies"] = hashlib.md5(repr([tuple(p) for p in policies]).encode()).hexdigest()
    out["forced_rls"] = (
        await _fetch("SELECT count(*) c FROM pg_class WHERE relrowsecurity AND relforcerowsecurity")
    )[0]["c"]
    return out


@pytest.fixture(scope="module")
def populated():
    async def setup():
        await H.run_migration("downgrade")
        return await H.seed(uuid.uuid4().hex[:8], scale=0.25)

    ds = H.run(setup())
    yield ds

    async def teardown():
        await H.cleanup(ds)
        await H.run_migration("upgrade")  # leave the DB as `alembic upgrade head` does

    H.run(teardown())


@pytest.fixture(autouse=True)
def _start_without_indexes(populated):
    H.run(H.run_migration("downgrade"))
    yield


def _assert_all_valid(idx):
    assert set(idx) == {f"ix_{t}_embedding_hnsw" for t in H.TABLES}
    for name, (valid, definition, _) in idx.items():
        assert valid, name
        assert _DEFINITION in definition, definition


async def test_upgrade_and_downgrade_preserve_data_and_rls(populated):
    before = await _fingerprint()
    assert await _indexes() == {}

    await H.run_migration("upgrade")
    _assert_all_valid(await _indexes())
    assert await _fingerprint() == before  # data, RLS policies, forced-RLS table count

    await H.run_migration("downgrade")
    assert await _indexes() == {}
    assert await _fingerprint() == before

    await H.run_migration("upgrade")  # and again: idempotent
    _assert_all_valid(await _indexes())
    await H.run_migration("upgrade")
    _assert_all_valid(await _indexes())


async def test_an_invalid_index_left_by_a_failed_build_is_rebuilt(populated):
    await H.run_migration("upgrade")
    name = "ix_code_chunks_embedding_hnsw"
    first_oid = (await _indexes())[name][2]
    # exactly what a cancelled CREATE INDEX CONCURRENTLY leaves behind
    await _fetch("UPDATE pg_index SET indisvalid = false WHERE indexrelid = $1::regclass", name)
    assert (await _indexes())[name][0] is False

    await H.run_migration("upgrade")

    idx = await _indexes()
    _assert_all_valid(idx)
    assert idx[name][2] != first_oid, "the invalid index must be dropped and rebuilt, not kept"


async def test_an_index_with_the_wrong_definition_is_replaced(populated):
    await H.run_migration("upgrade")
    name = "ix_incidents_chunks_embedding_hnsw"
    await _fetch(f"DROP INDEX {name}")
    # valid, but the wrong operator class (cosine): the query operator `<#>` would never use it
    await _fetch(
        f"CREATE INDEX {name} ON incidents_chunks USING hnsw (embedding vector_cosine_ops)"
    )
    assert "vector_cosine_ops" in (await _indexes())[name][1]

    await H.run_migration("upgrade")

    _assert_all_valid(await _indexes())


async def test_a_bounded_asyncpg_command_timeout_cancels_the_build_and_a_rerun_repairs_it(
    populated,
):
    """The regression behind `base.py`'s dedicated migration engine: the request
    engines carry a client-side asyncpg `command_timeout` (30 s), which cancels a
    real-sized HNSW build and leaves an INVALID index. Simulated with a 1 s bound
    (the seeded tables take several seconds to index); the migration engine
    (no timeout) then repairs it."""
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 -- driver-specific type
        await H.run_migration("upgrade", command_timeout=0.5)
    # Alembic's own `autocommit_block` exit then fails on the dead connection
    # (PendingRollbackError); the real cause is the chained timeout.
    chain, exc = [], excinfo.value
    while exc is not None and exc not in chain:
        chain.append(exc)
        exc = exc.__cause__ or exc.__context__
    assert any("timeout" in type(e).__name__.lower() for e in chain), [repr(e) for e in chain]

    idx = await _indexes()
    assert not (len(idx) == 4 and all(v[0] for v in idx.values())), (
        "the bounded run must not have succeeded"
    )

    await H.run_migration("upgrade")  # the unbounded migration engine
    _assert_all_valid(await _indexes())


async def test_ingestion_writes_continue_while_the_indexes_build(populated):
    """CONCURRENTLY: inserts proceed throughout the build (a plain CREATE INDEX
    would block every writer for its whole duration)."""
    rows = await _fetch(
        "SELECT organization_id o, project_id p, document_id d FROM code_chunks LIMIT 1"
    )
    o, p, d = rows[0]["o"], rows[0]["p"], rows[0]["d"]
    vec = "[" + ",".join(["0.05"] * H.DIM) + "]"
    done = asyncio.Event()
    inserted = 0

    async def writer():
        nonlocal inserted
        conn = await asyncpg.connect(H.asyncpg_dsn(H.SUPER_URL), ssl=False)
        try:
            while not done.is_set():
                await conn.execute(
                    "INSERT INTO code_chunks(organization_id,project_id,document_id,chunk_index,content,"  # noqa: E501
                    "embedding,source_offset_start,source_offset_end) "
                    "VALUES($1,$2,$3,$4,'w',$5::vector,0,1)",
                    o,
                    p,
                    d,
                    900000 + inserted,
                    vec,
                )
                inserted += 1
                await asyncio.sleep(0.02)
        finally:
            await conn.execute("DELETE FROM code_chunks WHERE chunk_index >= 900000")
            await conn.close()

    task = asyncio.create_task(writer())
    try:
        await H.run_migration("upgrade")
    finally:
        done.set()
        await task
    assert inserted >= 5, f"writers were blocked during the build ({inserted} inserts)"
    _assert_all_valid(await _indexes())


async def test_alembic_check_reports_zero_model_drift(populated):
    """The CI `alembic check` gate: models and the migrated database agree."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.database.models import (  # noqa: F401  (register every table)
        agent_models,
        auth_models,
        core_models,
        graph_models,
        ingestion_models,
        mcp_models,
        memory_models,
        pattern_models,
        retrieval_models,
        storage_models,
        tenancy_models,
    )

    await H.run_migration("upgrade")
    eng = create_async_engine(
        H.SUPER_URL.replace("?sslmode=disable", ""), connect_args={"ssl": False}
    )

    def _diff(sync_conn):
        return compare_metadata(MigrationContext.configure(sync_conn), Base.metadata)

    try:
        async with eng.connect() as conn:
            diffs = await conn.run_sync(_diff)
    finally:
        await eng.dispose()
    assert diffs == [], diffs
