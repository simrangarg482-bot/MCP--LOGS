"""Tests for `app.ingestion.workers.tasks.run_ingestion_job_task`'s
disconnected-connector handling (Phase: connector delete feature).

`core.tenancy.service.disconnect_connector` (the real backend behind the
frontend's "Delete connector" button) sets a connector's status to
`"disconnected"` rather than dropping its row (`ingestion_jobs.
connector_config_id` is `ON DELETE RESTRICT`). `app.ingestion.service.
_execute_ingestion_job` refuses to run for such a connector, raising a
`ConflictError` with `error_code="connector_config.disconnected"` before
any work starts. This suite covers the task wrapper's handling of that
specific error: it must NOT schedule a retry (an already-queued retry for a
job that timed out before the user deleted it would otherwise re-hit the
same slow sync a second and third time before giving up), while every
other exception shape must still retry exactly as before.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from arq.worker import Retry

from app.core.exceptions import ConflictError
from app.ingestion.workers import tasks as tasks_module


class _FakeSession:
    pass


def _fake_session_scope():
    @asynccontextmanager
    async def scope():
        yield _FakeSession()

    return scope()


@pytest.mark.asyncio
async def test_disconnected_connector_is_skipped_without_scheduling_a_retry(monkeypatch) -> None:
    async def fake_run_ingestion_job(session, connector_config_id):
        raise ConflictError(
            "This connector has been disconnected; ingestion will not run for it.",
            error_code="connector_config.disconnected",
        )

    monkeypatch.setattr(tasks_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(tasks_module.service, "run_ingestion_job", fake_run_ingestion_job)

    # Should return cleanly -- no Retry raised, no exception propagated.
    await tasks_module.run_ingestion_job_task({"job_try": 2, "job_id": "job-1"}, "11111111-1111-1111-1111-111111111111")


@pytest.mark.asyncio
async def test_other_setup_phase_failures_still_schedule_a_retry(monkeypatch) -> None:
    async def fake_run_ingestion_job(session, connector_config_id):
        raise RuntimeError("transient connection error")

    monkeypatch.setattr(tasks_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(tasks_module.service, "run_ingestion_job", fake_run_ingestion_job)

    with pytest.raises(Retry):
        await tasks_module.run_ingestion_job_task(
            {"job_try": 1, "job_id": "job-2"}, "22222222-2222-2222-2222-222222222222"
        )


@pytest.mark.asyncio
async def test_other_ekip_errors_still_schedule_a_retry(monkeypatch) -> None:
    """A `ConflictError` with a *different* error_code (e.g. a real,
    non-disconnected conflict) must not be swallowed by the disconnected-
    specific skip path -- only the exact `connector_config.disconnected`
    code is special-cased.
    """

    async def fake_run_ingestion_job(session, connector_config_id):
        raise ConflictError("Some other conflict.", error_code="ingestion.unsupported_source")

    monkeypatch.setattr(tasks_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(tasks_module.service, "run_ingestion_job", fake_run_ingestion_job)

    with pytest.raises(Retry):
        await tasks_module.run_ingestion_job_task(
            {"job_try": 1, "job_id": "job-3"}, "33333333-3333-3333-3333-333333333333"
        )
