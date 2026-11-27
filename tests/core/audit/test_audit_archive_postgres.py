"""Audit archival against real PostgreSQL as `ekip_app`: verified exports, never any deletion, tenant isolation,
idempotency and recoverable failures. Skipped unless EKIP_TEST_* point at a LOCAL migrated database."""

from __future__ import annotations

import gzip
import json
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.core.audit import archive
from tests.core.retention.test_retention_postgres import (  # noqa: F401  (fixtures, helpers, skip mark)
    _APP_URL,
    _SUPER_URL,
    _local,
    _org,
    _settings,
    env,
    pw,
    rt,
)

pytestmark = pytest.mark.skipif(
    not (_APP_URL and _SUPER_URL and _local(_APP_URL) and _local(_SUPER_URL)),
    reason="needs a LOCAL migrated database",
)

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


@pytest.fixture
async def au(rt):  # noqa: F811
    if not await rt.sup("SELECT 1 FROM information_schema.tables WHERE table_name='audit_archive_batches'"):
        pytest.skip("run `alembic upgrade head` on this database")
    yield rt
    await rt.sup("DELETE FROM audit_archive_batches WHERE organization_id IN (SELECT id FROM organizations WHERE slug LIKE :s)", s=f"rm-{rt.tag}-%")


async def _audit(rt, org, when, action="x.y", n=1):  # noqa: F811
    for i in range(n):
        await rt.sup(
            "INSERT INTO audit_logs (organization_id, actor, action, resource_type, metadata, occurred_at) "
            "VALUES (:o,'user:a',:a,'t', cast(:m as jsonb), :w)",
            o=org, a=f"{action}{i}", m=json.dumps({"i": i}), w=when,
        )


async def _total(rt, org):  # noqa: F811
    return (await rt.sup("SELECT count(*) FROM audit_logs WHERE organization_id=:o", o=org))[0][0]


def _read(path: Path):
    with gzip.open(path, "rb") as handle:
        return [json.loads(line) for line in handle]


@pytest.mark.asyncio
async def test_a_month_is_exported_verified_recorded_and_no_audit_row_is_touched(au, tmp_path) -> None:
    a, b = await _org(au, "aua"), await _org(au, "aub")
    await _audit(au, a, datetime(2025, 3, 10, tzinfo=UTC), n=7)
    await _audit(au, a, datetime(2025, 4, 2, tzinfo=UTC), n=2)       # a different month: its own file
    await _audit(au, b, datetime(2025, 3, 11, tzinfo=UTC), n=3)      # another organization
    before = (await _total(au, a), await _total(au, b))

    outcome = await archive.archive_month(au.scope, a, date(2025, 3, 1), directory=tmp_path, now=NOW, batch_size=3)
    assert outcome.status == "verified" and outcome.row_count == 7
    rows = _read(Path(outcome.location))
    assert len(rows) == 7 and {r["organization_id"] for r in rows} == {str(a)}  # only A's rows, never B's
    assert [r["action"] for r in rows] == [f"x.y{i}" for i in range(7)] or len({r["id"] for r in rows}) == 7
    assert Path(outcome.location).parent.name == str(a)  # one directory per organization

    ledger = (await au.sup("SELECT status, row_count, sha256, byte_size FROM audit_archive_batches WHERE organization_id=:o", o=a))[0]
    assert ledger[0] == "verified" and ledger[1] == 7 and ledger[2] == outcome.sha256 and ledger[3] > 0
    assert (await _total(au, a), await _total(au, b)) == before  # NOTHING was deleted or changed
    assert (await au.sup("SELECT count(*) FROM audit_archive_batches WHERE organization_id=:o", o=b))[0][0] == 0
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.asyncio
async def test_archiving_is_idempotent_and_a_tampered_file_is_detected_and_repaired(au, tmp_path) -> None:
    a = await _org(au, "auidem")
    await _audit(au, a, datetime(2025, 5, 3, tzinfo=UTC), n=4)
    first = await archive.archive_month(au.scope, a, date(2025, 5, 1), directory=tmp_path, now=NOW)
    again = await archive.archive_month(au.scope, a, date(2025, 5, 1), directory=tmp_path, now=NOW)
    assert (first.status, again.status) == ("verified", "skipped") and first.sha256 == again.sha256  # reproducible bytes

    assert [ok for _m, ok, _r in await archive.verify_archives(au.scope, a)] == [True]
    Path(first.location).write_bytes(b"corrupted")
    verdict = await archive.verify_archives(au.scope, a)
    assert verdict[0][1] is False and verdict[0][2] == "checksum mismatch"
    repaired = await archive.archive_month(au.scope, a, date(2025, 5, 1), directory=tmp_path, now=NOW)  # a bad file is re-exported
    assert repaired.status == "verified" and [ok for _m, ok, _r in await archive.verify_archives(au.scope, a)] == [True]


@pytest.mark.asyncio
async def test_a_failure_leaves_no_partial_archive_is_recorded_and_recovers_on_the_next_run(au, tmp_path, monkeypatch) -> None:
    a = await _org(au, "aufail")
    await _audit(au, a, datetime(2025, 6, 3, tzinfo=UTC), n=5)
    original = archive.verify_file

    def broken(*args, **kwargs):
        return False, "simulated corruption"

    monkeypatch.setattr(archive, "verify_file", broken)
    failed = await archive.archive_month(au.scope, a, date(2025, 6, 1), directory=tmp_path, now=NOW)
    assert failed.status == "failed"
    assert not list(tmp_path.rglob("*.ndjson.gz")) and not list(tmp_path.rglob("*.tmp"))  # no half-written file survives
    assert (await au.sup("SELECT status FROM audit_archive_batches WHERE organization_id=:o", o=a))[0][0] == "failed"
    assert await _total(au, a) == 5
    monkeypatch.setattr(archive, "verify_file", original)
    ok = await archive.archive_month(au.scope, a, date(2025, 6, 1), directory=tmp_path, now=NOW)
    assert ok.status == "verified"
    assert (await au.sup("SELECT status, error FROM audit_archive_batches WHERE organization_id=:o", o=a))[0] == ("verified", None)


@pytest.mark.asyncio
async def test_only_completed_old_months_are_eligible_and_dry_run_writes_nothing(au, tmp_path) -> None:
    a = await _org(au, "auplan")
    await _audit(au, a, datetime(2024, 1, 15, tzinfo=UTC), n=2)      # old: eligible
    await _audit(au, a, datetime(2026, 10, 15, tzinfo=UTC), n=2)     # last month: inside the 365-day minimum age
    await _audit(au, a, datetime(2026, 11, 20, tzinfo=UTC), n=2)     # still running
    settings = _settings(audit_archive_directory=str(tmp_path), audit_archive_min_age_days=365)
    plan = await archive.run_archive(au.scope, organization_ids=[a], settings=settings, apply=False, now=NOW)
    assert [(o.period_start, o.status) for o in plan.outcomes] == [(date(2024, 1, 1), "planned")]
    assert not list(tmp_path.rglob("*"))  # a dry run writes nothing, not even a directory
    done = await archive.run_archive(au.scope, organization_ids=[a], settings=settings, apply=True, now=NOW)
    assert [o.status for o in done.outcomes] == ["verified"] and not done.failed
    assert await _total(au, a) == 6  # still everything
    with pytest.raises(ValueError):  # a month that has not ended can never be archived
        await archive.archive_month(au.scope, a, date(2026, 11, 1), directory=tmp_path, now=NOW)


@pytest.mark.asyncio
async def test_archiving_requires_a_configured_directory(au) -> None:
    with pytest.raises(ValueError, match="AUDIT_ARCHIVE_DIRECTORY"):
        await archive.run_archive(au.scope, organization_ids=[], settings=_settings(audit_archive_directory=None), apply=True, now=NOW)


@pytest.mark.asyncio
async def test_the_ledger_is_tenant_isolated(au, tmp_path) -> None:
    a, b = await _org(au, "auleda"), await _org(au, "aulegb")
    await _audit(au, a, datetime(2025, 7, 3, tzinfo=UTC), n=2)
    await archive.archive_month(au.scope, a, date(2025, 7, 1), directory=tmp_path, now=NOW)

    async def visible(session, org):
        from sqlalchemy import text

        from app.database.session import set_tenant_context

        await set_tenant_context(session, org)
        return (await session.execute(text("SELECT count(*) FROM audit_archive_batches WHERE organization_id = :a"), {"a": a})).scalar_one()

    async with au.scope() as session:
        assert await visible(session, b) == 0  # B's context cannot see A's ledger row
    async with au.scope() as session:
        assert await visible(session, a) == 1
    assert await archive.verify_archives(au.scope, b) == []
