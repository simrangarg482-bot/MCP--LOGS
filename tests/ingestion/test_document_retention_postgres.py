"""Phases 2-4 against real PostgreSQL as `ekip_app`: dead-chunk cleanup across all four chunk tables, version
tombstones that keep every citation resolvable, and connector-deletion cleanup that never touches another
connector or organization.

Skipped unless EKIP_TEST_* point at a LOCAL migrated database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.ingestion import retention as doc_retention
from app.ingestion.lifecycle import CHUNK_TABLES
from tests.core.retention.test_retention_postgres import (  # noqa: F401  (fixtures, helpers, skip mark)
    _APP_URL,
    _SUPER_URL,
    _ago,
    _local,
    _nosleep,
    _org,
    _settings,
    _user,
    env,
    pw,
    rt,
)

pytestmark = pytest.mark.skipif(
    not (_APP_URL and _SUPER_URL and _local(_APP_URL) and _local(_SUPER_URL)),
    reason="needs a LOCAL migrated database",
)

NOW = datetime.now(UTC)


async def _project(rt, org):  # noqa: F811
    return (await rt.sup("SELECT id FROM projects WHERE organization_id=:o LIMIT 1", o=org))[0][0]


async def _doc(rt, org, project, *, key="k", version=1, source="github", superseded_days=None, deleted_days=None,  # noqa: F811
               tables=("documentation_chunks",), meta=False):
    doc = uuid.uuid4()
    await rt.sup(
        "INSERT INTO documents (id, organization_id, project_id, source, external_id, content_hash, status, version, "
        "superseded_at, deleted_at, title) VALUES (:i,:o,:p,:s,:k,:h,'published',:v,:sa,:da,'title')",
        i=doc, o=org, p=project, s=source, k=key, h=uuid.uuid4().hex, v=version,
        sa=_ago(superseded_days) if superseded_days is not None else None,
        da=_ago(deleted_days) if deleted_days is not None else None,
    )
    for table in tables:
        await rt.sup(
            f"INSERT INTO {table} (organization_id, project_id, document_id, chunk_index, content, embedding, "
            "source_offset_start, source_offset_end) VALUES (:o,:p,:d,0,'text', "
            "array_fill(0.1::real, ARRAY[384])::vector, 0, 4)",
            o=org, p=project, d=doc,
        )
    if meta:
        await rt.sup("INSERT INTO document_metadata (document_id, key, value) VALUES (:d,'repo','x')", d=doc)
    return doc


async def _chunks(rt, doc):  # noqa: F811
    return {t: (await rt.sup(f"SELECT count(*) FROM {t} WHERE document_id=:d", d=doc))[0][0] for t in CHUNK_TABLES}


async def _cleanup_docs(rt):  # noqa: F811
    s = f"rm-{rt.tag}-%"
    for t in CHUNK_TABLES:
        await rt.sup(f"DELETE FROM {t} WHERE organization_id IN (SELECT id FROM organizations WHERE slug LIKE :s)", s=s)
    await rt.sup("DELETE FROM document_metadata WHERE document_id IN (SELECT id FROM documents WHERE organization_id IN (SELECT id FROM organizations WHERE slug LIKE :s))", s=s)
    await rt.sup("DELETE FROM documents WHERE organization_id IN (SELECT id FROM organizations WHERE slug LIKE :s)", s=s)


@pytest.fixture
async def dr(rt):  # noqa: F811
    yield rt
    await _cleanup_docs(rt)


ALL4 = tuple(CHUNK_TABLES)


# ================================================================ Phase 2: dead chunks


@pytest.mark.asyncio
async def test_dead_chunks_of_superseded_and_old_soft_deleted_documents_are_purged_from_all_four_tables(dr) -> None:
    a, b = await _org(dr, "dca"), await _org(dr, "dcb")
    pa, pb = await _project(dr, a), await _project(dr, b)
    current = await _doc(dr, a, pa, key="cur", tables=ALL4)
    superseded = await _doc(dr, a, pa, key="old", version=1, superseded_days=3, tables=ALL4)
    old_deleted = await _doc(dr, a, pa, key="del", deleted_days=10, tables=ALL4)
    fresh_deleted = await _doc(dr, a, pa, key="freshdel", deleted_days=1, tables=ALL4)  # inside the 7-day grace
    other_org_dead = await _doc(dr, b, pb, key="old", version=1, superseded_days=3, tables=ALL4)
    # an earlier failure left TWO "current" versions of one logical document: the older must be healed and purged
    healed_old = await _doc(dr, a, pa, key="missed", version=1, tables=ALL4)
    healed_new = await _doc(dr, a, pa, key="missed", version=2, tables=("documentation_chunks",))
    rows_before = (await dr.sup("SELECT count(*) FROM documents WHERE organization_id IN (:a,:b)", a=a, b=b))[0][0]

    marked, purged = await doc_retention.purge_dead_chunks(dr.scope, [a, b], settings=_settings(), now=NOW, sleep_seconds=0)

    assert marked == 1
    for dead in (superseded, old_deleted, other_org_dead, healed_old):
        assert set((await _chunks(dr, dead)).values()) == {0}, dead  # every one of the four tables is clean
    assert (await _chunks(dr, current)) == {t: 1 for t in ALL4}  # active version untouched
    assert (await _chunks(dr, fresh_deleted)) == {t: 1 for t in ALL4}  # still inside the grace period
    assert (await _chunks(dr, healed_new))["documentation_chunks"] == 1
    assert (await dr.sup("SELECT count(*) FROM documents WHERE organization_id IN (:a,:b)", a=a, b=b))[0][0] == rows_before  # no document row deleted
    assert sum(purged.values()) == 4 * 4  # org A: superseded + old-deleted + healed (3 x 4 tables); org B: 1 x 4
    again_marked, again = await doc_retention.purge_dead_chunks(dr.scope, [a, b], settings=_settings(), now=NOW, sleep_seconds=0)
    assert again_marked == 0 and sum(again.values()) == 0  # idempotent


@pytest.mark.asyncio
async def test_dead_chunk_dry_run_reports_without_deleting(dr) -> None:
    a = await _org(dr, "dcdry")
    pa = await _project(dr, a)
    dead = await _doc(dr, a, pa, key="old", version=1, superseded_days=3, tables=ALL4)
    _marked, counts = await doc_retention.purge_dead_chunks(dr.scope, [a], settings=_settings(retention_dry_run=True), now=NOW, sleep_seconds=0)
    assert sum(counts.values()) == 4
    assert (await _chunks(dr, dead)) == {t: 1 for t in ALL4}


@pytest.mark.asyncio
async def test_a_failure_in_one_organization_does_not_stop_the_others(dr, monkeypatch) -> None:
    a, b = await _org(dr, "dcfa"), await _org(dr, "dcfb")
    pa, pb = await _project(dr, a), await _project(dr, b)
    da = await _doc(dr, a, pa, key="old", version=1, superseded_days=3, tables=("documentation_chunks",))
    db = await _doc(dr, b, pb, key="old", version=1, superseded_days=3, tables=("documentation_chunks",))
    from app.ingestion import lifecycle

    original = lifecycle.run_backfill

    async def flaky(scope, *, organization_ids, **kw):
        if organization_ids == [a]:
            raise RuntimeError("simulated")
        return await original(scope, organization_ids=organization_ids, **kw)

    monkeypatch.setattr(lifecycle, "run_backfill", flaky)
    await doc_retention.purge_dead_chunks(dr.scope, [a, b], settings=_settings(), now=NOW, sleep_seconds=0)
    assert (await _chunks(dr, da))["documentation_chunks"] == 1  # a failed, nothing half-done
    assert (await _chunks(dr, db))["documentation_chunks"] == 0  # b still cleaned
    monkeypatch.setattr(lifecycle, "run_backfill", original)
    await doc_retention.purge_dead_chunks(dr.scope, [a, b], settings=_settings(), now=NOW, sleep_seconds=0)
    assert (await _chunks(dr, da))["documentation_chunks"] == 0  # the retry converges


# ================================================================ Phase 3: version tombstones


@pytest.mark.asyncio
async def test_old_versions_are_tombstoned_but_every_document_row_and_citation_stays(dr) -> None:
    a, b = await _org(dr, "vta"), await _org(dr, "vtb")
    pa, pb = await _project(dr, a), await _project(dr, b)
    # one logical document with 8 versions: v8 current, v1..v7 superseded progressively more recently
    ids = {}
    for v in range(1, 9):
        ids[v] = await _doc(dr, a, pa, key="x", version=v, superseded_days=None if v == 8 else 400 - v * 10, meta=True)
    recent = await _doc(dr, a, pa, key="y", version=1, superseded_days=5, meta=True)   # superseded 5 days ago
    for v in range(2, 8):
        await _doc(dr, a, pa, key="y", version=v, superseded_days=4 if v < 7 else None, meta=True)
    manual = await _doc(dr, a, pa, key="m", source="manual", version=1, meta=True)
    other = await _doc(dr, b, pb, key="x", version=1, superseded_days=300, meta=True)
    for v in range(2, 9):
        await _doc(dr, b, pb, key="x", version=v, superseded_days=None if v == 8 else 250, meta=True)

    total = await doc_retention.tombstone_old_versions(dr.scope, [a, b], settings=_settings(document_version_keep_latest=5, document_version_retention_days=90), now=NOW, sleep=_nosleep)
    assert total == 3 + 3  # v1..v3 of org A's "x" and of org B's "x"

    async def meta(doc):
        return (await dr.sup("SELECT count(*) FROM document_metadata WHERE document_id=:d", d=doc))[0][0]

    async def tomb(doc):
        return (await dr.sup("SELECT tombstoned_at IS NOT NULL FROM documents WHERE id=:d", d=doc))[0][0]

    for v in (1, 2, 3):
        assert await tomb(ids[v]) and await meta(ids[v]) == 0
    for v in (4, 5, 6, 7, 8):  # the newest five (v8 current + four superseded) keep their metadata
        assert not await tomb(ids[v]) and await meta(ids[v]) == 1
    assert not await tomb(recent) and await meta(recent) == 1   # outside the newest N but superseded only 5 days ago
    assert not await tomb(manual)                               # manual documents are never versioned this way
    assert await tomb(other)                                    # the other organization is handled in ITS OWN context
    # no row was deleted: every id (a citation) still resolves to its title / URL / hash
    resolved = await dr.sup("SELECT id, title FROM documents WHERE id = ANY(:ids)", ids=list(ids.values()))
    assert len(resolved) == 8 and all(r[1] == "title" for r in resolved)
    assert await doc_retention.tombstone_old_versions(dr.scope, [a, b], settings=_settings(document_version_keep_latest=5, document_version_retention_days=90), now=NOW, sleep=_nosleep) == 0  # idempotent


@pytest.mark.asyncio
async def test_a_tombstoned_version_that_becomes_current_again_is_healed(dr) -> None:
    """An A -> B -> A revert makes the old row current again (ingestion's own code does that and is not modified).
    The daily job clears the tombstone flag: `tombstoned_at` set while `superseded_at` is NULL is never valid."""
    a = await _org(dr, "vtrev")
    pa = await _project(dr, a)
    old = await _doc(dr, a, pa, key="x", version=1, superseded_days=300, meta=True)
    for v in range(2, 9):
        await _doc(dr, a, pa, key="x", version=v, superseded_days=None if v == 8 else 250)
    await doc_retention.tombstone_old_versions(dr.scope, [a], settings=_settings(document_version_keep_latest=5, document_version_retention_days=90), now=NOW, sleep=_nosleep)
    assert (await dr.sup("SELECT tombstoned_at IS NOT NULL FROM documents WHERE id=:d", d=old))[0][0]
    # what ingestion's revive does to the row: current again, at the new highest version
    await dr.sup("UPDATE documents SET superseded_at = NULL, version = 9 WHERE id=:d", d=old)
    healed = await doc_retention.heal_revived_tombstones(dr.scope, [a], settings=_settings(), sleep=_nosleep)
    assert healed == 1
    state = (await dr.sup("SELECT tombstoned_at, superseded_at, version FROM documents WHERE id=:d", d=old))[0]
    assert state[0] is None and state[1] is None and state[2] == 9
    # tombstones of versions that are still superseded are left alone
    assert (await dr.sup("SELECT count(*) FROM documents WHERE organization_id=:o AND tombstoned_at IS NOT NULL", o=a))[0][0] == 2
    assert await doc_retention.heal_revived_tombstones(dr.scope, [a], settings=_settings(), sleep=_nosleep) == 0  # idempotent


# ================================================================ Phase 4: connector deletion


async def _connector(dr, org, source, status):  # noqa: F811
    cid = uuid.uuid4()
    await dr.sup(
        "INSERT INTO connector_configs (id, organization_id, source, credential_ref, config, status) VALUES (:i,:o,:s,'x','{}'::jsonb,:st)",
        i=cid, o=org, s=source, st=status,
    )
    return cid


@pytest.mark.asyncio
async def test_cleanup_removes_the_only_connector_of_its_kinds_documents_and_nothing_else(dr) -> None:
    a, b = await _org(dr, "cca"), await _org(dr, "ccb")
    pa, pb = await _project(dr, a), await _project(dr, b)
    await _connector(dr, a, "slack", "active")                       # a different kind: its documents stay
    gh = await _connector(dr, a, "github", "disconnected")           # the ONLY github connector of organization A
    mine = [await _doc(dr, a, pa, key=f"m{i}", source="github", tables=ALL4) for i in range(5)]
    slack = await _doc(dr, a, pa, key="s", source="slack", tables=ALL4)
    manual = await _doc(dr, a, pa, key="man", source="manual", tables=ALL4)
    foreign = await _doc(dr, b, pb, key="f", source="github", tables=ALL4)  # another organization's github documents
    outcome = await doc_retention.cleanup_connector_documents(dr.scope, a, gh, batch_size=2, sleep=_nosleep, pause_seconds=0)
    assert outcome == (5, 5 * 4)
    for d in mine:
        assert set((await _chunks(dr, d)).values()) == {0}
        assert (await dr.sup("SELECT deleted_at IS NOT NULL FROM documents WHERE id=:d", d=d))[0][0]  # soft-deleted, row kept
    for d in (slack, manual, foreign):
        assert (await _chunks(dr, d)) == {t: 1 for t in ALL4}
        assert (await dr.sup("SELECT deleted_at IS NULL FROM documents WHERE id=:d", d=d))[0][0]
    assert await doc_retention.cleanup_connector_documents(dr.scope, a, gh, batch_size=2, sleep=_nosleep, pause_seconds=0) == (0, 0)  # idempotent


@pytest.mark.asyncio
async def test_with_two_connectors_of_the_same_kind_nothing_is_deleted(dr) -> None:
    """Documents do not record their connector, so with two github connectors they cannot be told apart. The
    cleanup refuses to guess: it deletes nothing."""
    a = await _org(dr, "ccamb")
    pa = await _project(dr, a)
    gone = await _connector(dr, a, "github", "disconnected")
    await _connector(dr, a, "github", "active")
    doc = await _doc(dr, a, pa, key="k", source="github", tables=ALL4)
    assert await doc_retention.cleanup_connector_documents(dr.scope, a, gone, sleep=_nosleep, pause_seconds=0) is None
    assert (await _chunks(dr, doc)) == {t: 1 for t in ALL4}
    assert (await dr.sup("SELECT deleted_at IS NULL FROM documents WHERE id=:d", d=doc))[0][0]
    connectors, documents, chunks, errors = await doc_retention.sweep_disconnected_connectors(dr.scope, [a], settings=_settings(), sleep=_nosleep)
    assert (connectors, documents, chunks, errors) == (0, 0, 0, [])  # the daily sweep skips it too


@pytest.mark.asyncio
async def test_an_active_or_foreign_connector_is_never_cleaned(dr) -> None:
    a, b = await _org(dr, "ccn"), await _org(dr, "ccm")
    pa = await _project(dr, a)
    active = await _connector(dr, a, "github", "active")
    doc = await _doc(dr, a, pa, key="k", source="github", tables=ALL4)
    assert await doc_retention.cleanup_connector_documents(dr.scope, a, active, sleep=_nosleep, pause_seconds=0) is None
    assert await doc_retention.cleanup_connector_documents(dr.scope, b, active, sleep=_nosleep, pause_seconds=0) is None  # wrong organization
    assert (await _chunks(dr, doc)) == {t: 1 for t in ALL4}


@pytest.mark.asyncio
async def test_connector_cleanup_can_be_retried_after_a_failure_and_the_daily_sweep_finishes_it(dr) -> None:
    a = await _org(dr, "ccr")
    pa = await _project(dr, a)
    c = await _connector(dr, a, "github", "disconnected")
    docs = [await _doc(dr, a, pa, key=f"k{i}", source="github", tables=ALL4) for i in range(6)]

    class Boom(Exception):
        pass

    calls = {"n": 0}

    async def explode(_):
        calls["n"] += 1
        raise Boom

    with pytest.raises(Boom):  # dies after the first batch of the first phase
        await doc_retention.cleanup_connector_documents(dr.scope, a, c, batch_size=2, sleep=explode, pause_seconds=0)
    assert calls["n"] == 1
    # a retry (here: the daily sweep) completes the job; nothing is done twice
    connectors, documents, chunks, errors = await doc_retention.sweep_disconnected_connectors(dr.scope, [a], settings=_settings(), sleep=_nosleep)
    assert (connectors, errors) == (1, [])
    for d in docs:
        assert set((await _chunks(dr, d)).values()) == {0}
        assert (await dr.sup("SELECT deleted_at IS NOT NULL FROM documents WHERE id=:d", d=d))[0][0]
