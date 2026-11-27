"""Storage monitoring against real PostgreSQL as `ekip_app`: measured totals, configured-limit warning, honest
tenant ESTIMATES, growth between snapshots, tenant isolation, platform-operator gating and pruning."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import PermissionDeniedError
from app.core.storage import service as storage
from tests.core.retention.test_retention_postgres import (  # noqa: F401  (fixtures, helpers, skip mark)
    _APP_URL,
    _SUPER_URL,
    _local,
    _org,
    _settings,
    _user,
    env,
    pw,
    rt,
)
from tests.ingestion.test_document_retention_postgres import ALL4, _cleanup_docs, _doc, _project  # noqa: F401

pytestmark = pytest.mark.skipif(
    not (_APP_URL and _SUPER_URL and _local(_APP_URL) and _local(_SUPER_URL)),
    reason="needs a LOCAL migrated database",
)


@pytest.fixture
async def st(rt):  # noqa: F811
    if not await rt.sup("SELECT 1 FROM information_schema.tables WHERE table_name='storage_snapshots'"):
        pytest.skip("run `alembic upgrade head` on this database")
    await rt.sup("DELETE FROM storage_snapshots")  # a throw-away test database: start from a known state
    yield rt
    await rt.sup("DELETE FROM storage_snapshots")
    await _cleanup_docs(rt)


@pytest.mark.asyncio
async def test_snapshot_records_measured_sizes_and_no_percentage_without_a_configured_limit(st) -> None:
    result = await storage.take_snapshot(st.scope, settings=_settings(database_storage_limit_bytes=None))
    assert result.database_bytes > 0 and result.percent_of_limit is None and result.warning is False  # never guesses a limit
    row = (await st.sup("SELECT database_bytes, storage_limit_bytes, percent_of_limit, warning FROM storage_snapshots"))[0]
    assert row[0] == result.database_bytes and row[1] is None and row[2] is None and row[3] is False
    tables = {r[0]: r for r in await st.sup("SELECT table_name, category, total_bytes, table_bytes, index_bytes FROM storage_snapshot_tables")}
    assert {"documentation_chunks", "code_chunks", "conversations_chunks", "incidents_chunks"} <= set(tables)
    assert tables["documentation_chunks"][1] == "chunks" and tables["documents"][1] == "documents" and tables["audit_logs"][1] == "audit"
    assert all(r[2] >= r[3] for r in tables.values())  # total includes the table itself


@pytest.mark.asyncio
async def test_the_warning_fires_at_the_configured_threshold_only(st) -> None:
    size = (await st.sup("SELECT pg_database_size(current_database())"))[0][0]
    above = await storage.take_snapshot(st.scope, settings=_settings(database_storage_limit_bytes=int(size / 0.8), storage_warning_percent=70))
    assert above.warning is True and 70 <= above.percent_of_limit <= 90
    below = await storage.take_snapshot(st.scope, settings=_settings(database_storage_limit_bytes=int(size / 0.4), storage_warning_percent=70))
    assert below.warning is False and below.percent_of_limit < 70
    flags = [r[0] for r in await st.sup("SELECT warning FROM storage_snapshots ORDER BY taken_at")]
    assert flags == [True, False]


@pytest.mark.asyncio
async def test_tenant_estimates_are_proportional_flagged_and_confined_to_the_organization(st) -> None:
    a, b = await _org(st, "sta"), await _org(st, "stb")
    pa, pb = await _project(st, a), await _project(st, b)
    for i in range(3):
        await _doc(st, a, pa, key=f"a{i}", tables=ALL4)
    await _doc(st, b, pb, key="b0", tables=ALL4)
    result = await storage.take_snapshot(st.scope, settings=_settings())
    assert result.tenants >= 2

    mine = await st.run(a, lambda s: storage.get_tenant_storage(s, a))
    theirs = await st.run(b, lambda s: storage.get_tenant_storage(s, b))
    assert mine.available and mine.is_estimate and mine.basis == storage.TENANT_BASIS
    assert (mine.chunk_rows, mine.document_rows) == (12, 3) and (theirs.chunk_rows, theirs.document_rows) == (4, 1)  # exact counts
    assert mine.estimated_bytes is not None and theirs.estimated_bytes is not None
    assert mine.estimated_bytes > theirs.estimated_bytes  # 3x the rows -> the larger share
    total_estimated = (await st.sup("SELECT sum(estimated_bytes) FROM storage_snapshot_tenants WHERE snapshot_id=:s", s=result.snapshot_id))[0][0]
    measured_chunk_doc = (await st.sup(
        "SELECT sum(total_bytes) FROM storage_snapshot_tables WHERE snapshot_id=:s AND category IN ('chunks','documents')", s=result.snapshot_id))[0][0]
    assert total_estimated <= measured_chunk_doc  # estimates can never add up to more than what was measured

    # RLS: under A's context only A's tenant rows exist; B's are invisible even by explicit id
    async def visible(s):
        from sqlalchemy import text

        from app.database.session import set_tenant_context

        await set_tenant_context(s, a)
        return (await s.execute(text("SELECT count(*) FROM storage_snapshot_tenants WHERE organization_id = :b"), {"b": b})).scalar_one()

    async with st.scope() as session:
        assert await visible(session) == 0
    nobody = await st.run(None, lambda s: storage.get_tenant_storage(s, uuid.uuid4()))
    assert nobody.available is False  # an organization with no snapshot row reports "not available", never someone else's


@pytest.mark.asyncio
async def test_tenant_estimates_can_be_switched_off(st) -> None:
    await _org(st, "stoff")
    result = await storage.take_snapshot(st.scope, settings=_settings(storage_tenant_estimates_enabled=False))
    assert result.tenants == 0
    assert (await st.sup("SELECT count(*) FROM storage_snapshot_tenants WHERE snapshot_id=:s", s=result.snapshot_id))[0][0] == 0


@pytest.mark.asyncio
async def test_growth_is_measured_between_snapshots_and_old_snapshots_are_pruned(st) -> None:
    now = datetime.now(UTC)
    for days_ago, size in ((400, 10), (30, 1_000), (8, 5_000)):
        await st.sup(
            "INSERT INTO storage_snapshots (taken_at, database_bytes) VALUES (:t, :b)", t=now - timedelta(days=days_ago), b=size
        )
    result = await storage.take_snapshot(st.scope, settings=_settings(storage_snapshot_retention_days=200), now=now)
    assert result.pruned == 1  # the 400-day-old one only
    operator = await _user(st, "stop")
    await st.sup("INSERT INTO platform_operators (user_id, role, granted_by) VALUES (:u,'viewer',:u)", u=operator)
    try:
        overview = await st.run(None, lambda s: storage.get_platform_storage(s, operator, now=now))
    finally:
        await st.sup("DELETE FROM platform_operators WHERE user_id=:u", u=operator)
    by_days = {g.days: g for g in overview.growth}
    assert by_days[7].bytes_added == result.database_bytes - 5_000 and by_days[7].bytes_per_day > 0  # measured difference
    assert by_days[30].bytes_added == result.database_bytes - 1_000
    assert overview.available and overview.database_bytes == result.database_bytes and overview.series


@pytest.mark.asyncio
async def test_dry_run_still_snapshots_but_never_prunes(st) -> None:
    await st.sup("INSERT INTO storage_snapshots (taken_at, database_bytes) VALUES (:t, 1)", t=datetime.now(UTC) - timedelta(days=900))
    result = await storage.take_snapshot(st.scope, settings=_settings(storage_snapshot_retention_days=30, retention_dry_run=True))
    assert result.pruned == 0
    assert (await st.sup("SELECT count(*) FROM storage_snapshots WHERE database_bytes = 1"))[0][0] == 1


@pytest.mark.asyncio
async def test_only_an_active_platform_operator_can_read_the_platform_overview(st) -> None:
    await storage.take_snapshot(st.scope, settings=_settings())
    stranger = await _user(st, "ststranger")
    with pytest.raises(PermissionDeniedError):
        await st.run(None, lambda s: storage.get_platform_storage(s, stranger))
    a = await _org(st, "stp")
    pa = await _project(st, a)
    await _doc(st, a, pa, key="x", tables=ALL4)
    operator = await _user(st, "stop2")
    await st.sup("INSERT INTO platform_operators (user_id, role, granted_by) VALUES (:u,'viewer',:u)", u=operator)
    try:
        await storage.take_snapshot(st.scope, settings=_settings())
        overview = await st.run(None, lambda s: storage.get_platform_storage(s, operator))
        assert any(t.organization_id == a and t.is_estimate for t in overview.tenants)  # flagged as estimates
        assert all(t.basis == storage.TENANT_BASIS for t in overview.tenants)
        assert overview.categories.get("chunks", 0) > 0
    finally:
        await st.sup("DELETE FROM platform_operators WHERE user_id=:u", u=operator)
