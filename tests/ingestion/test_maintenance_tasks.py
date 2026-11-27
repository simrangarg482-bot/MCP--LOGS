"""The scheduled maintenance job and the connector-cleanup task: registration, switches and failure isolation
(no database; the retention services are replaced by recorders)."""

from __future__ import annotations

import uuid
import pytest
from arq.worker import Retry

from app.ingestion.workers import main as worker_main
from app.ingestion.workers import tasks
from app.shared.config.settings import get_settings


def _cron(name: str):
    return next(c for c in worker_main.WorkerSettings.cron_jobs if c.name == f"cron:{name}")


def test_the_daily_maintenance_job_and_the_cleanup_task_are_registered() -> None:
    names = {c.name for c in worker_main.WorkerSettings.cron_jobs}
    assert names == {"cron:scheduled_reconciliation", "cron:scheduled_maintenance"}  # the hourly job is unchanged
    maintenance = _cron("scheduled_maintenance")
    assert maintenance.hour == get_settings().retention_hour and maintenance.minute == 15  # once a day
    assert maintenance.unique is True and maintenance.timeout_s >= 3600
    assert _cron("scheduled_reconciliation").minute == 0 and _cron("scheduled_reconciliation").hour is None
    assert {f.__name__ for f in worker_main.WorkerSettings.functions} == {
        "run_ingestion_job_task", "cleanup_connector_documents_task",
    }
    assert not hasattr(worker_main.WorkerSettings, "timezone")  # the worker's own settings are untouched


@pytest.mark.asyncio
async def test_maintenance_is_off_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(tasks, "get_settings", lambda: get_settings().model_copy(update={"retention_enabled": False}))

    async def never(*a, **k):
        raise AssertionError("must not run")

    monkeypatch.setattr(tasks.retention_service, "run_retention", never)
    monkeypatch.setattr(tasks.document_retention, "run_document_cleanup", never)
    assert await tasks.scheduled_maintenance({}) == {"status": "disabled"}


@pytest.mark.asyncio
async def test_a_failing_step_does_not_prevent_the_next_one(monkeypatch) -> None:
    monkeypatch.setattr(tasks, "get_settings", lambda: get_settings().model_copy(update={"retention_enabled": True}))
    calls = []

    async def failing(*a, **k):
        calls.append("retention")
        raise RuntimeError("boom")

    class Report:
        chunks_purged = {"documentation_chunks": 3}
        versions_tombstoned = 2
        errors: list = []

    async def documents(*a, **k):
        calls.append("documents")
        return Report()

    class Snap:
        database_bytes = 123
        warning = False

    async def snapshot(*a, **k):
        calls.append("storage")
        return Snap()

    monkeypatch.setattr(tasks.retention_service, "run_retention", failing)
    monkeypatch.setattr(tasks.document_retention, "run_document_cleanup", documents)
    monkeypatch.setattr(tasks.storage_service, "take_snapshot", snapshot)
    summary = await tasks.scheduled_maintenance({})
    assert calls == ["retention", "documents", "storage"]  # every step ran although the first failed
    assert summary["database_bytes"] == 123
    assert summary["retention_error"] is True and summary["document_chunks_purged"] == 3 and summary["versions_tombstoned"] == 2


@pytest.mark.asyncio
async def test_cleanup_task_retries_with_backoff_then_leaves_it_to_the_daily_sweep(monkeypatch) -> None:
    async def failing(*a, **k):
        raise RuntimeError("db blip")

    monkeypatch.setattr(tasks.document_retention, "cleanup_connector_documents", failing)
    org, cid = str(uuid.uuid4()), str(uuid.uuid4())
    with pytest.raises(Retry) as err:
        await tasks.cleanup_connector_documents_task({"job_try": 1}, org, cid)
    assert err.value.defer_score > 0
    last = await tasks.cleanup_connector_documents_task({"job_try": tasks.MAX_JOB_TRIES}, org, cid)
    assert last == {"status": "failed", "retry": "daily_sweep"}


@pytest.mark.asyncio
async def test_cleanup_task_results(monkeypatch) -> None:
    org, cid = str(uuid.uuid4()), str(uuid.uuid4())
    seen = {}

    async def fake(scope, organization_id, connector_config_id, **k):
        seen["ids"] = (organization_id, connector_config_id)
        return (3, 12)

    monkeypatch.setattr(tasks.document_retention, "cleanup_connector_documents", fake)
    assert await tasks.cleanup_connector_documents_task({}, org, cid) == {
        "status": "cleaned", "documents_deleted": 3, "chunks_deleted": 12,
    }
    assert seen["ids"] == (uuid.UUID(org), uuid.UUID(cid))

    async def none(*a, **k):
        return None

    monkeypatch.setattr(tasks.document_retention, "cleanup_connector_documents", none)
    assert await tasks.cleanup_connector_documents_task({}, org, cid) == {"status": "skipped"}
