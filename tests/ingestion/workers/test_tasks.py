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


class _FakeRedis:
    def __init__(self, *, acquired: bool) -> None:
        self.acquired = acquired
        self.set_calls: list[tuple] = []
        self.eval_calls: list[tuple] = []

    async def set(self, *args, **kwargs):
        self.set_calls.append((args, kwargs))
        return self.acquired

    async def eval(self, *args):
        self.eval_calls.append(args)
        return 1


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


@pytest.mark.asyncio
async def test_connector_lock_skips_duplicate_job_before_service_call(monkeypatch) -> None:
    redis = _FakeRedis(acquired=False)
    service_called = False

    async def fake_run_ingestion_job(session, connector_config_id):
        nonlocal service_called
        service_called = True
        raise AssertionError("duplicate must not enter ingestion service")

    monkeypatch.setattr(tasks_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(tasks_module.service, "run_ingestion_job", fake_run_ingestion_job)

    await tasks_module.run_ingestion_job_task(
        {"job_try": 1, "job_id": "duplicate", "redis": redis},
        "44444444-4444-4444-4444-444444444444",
    )

    assert service_called is False
    assert len(redis.set_calls) == 1
    assert redis.eval_calls == []


@pytest.mark.asyncio
async def test_connector_lock_release_is_token_guarded() -> None:
    redis = _FakeRedis(acquired=True)
    lock = await tasks_module._acquire_connector_lock(
        {"job_id": "job-locked", "redis": redis},
        "55555555-5555-5555-5555-555555555555",
    )

    assert lock is not None
    await tasks_module._release_connector_lock(lock)

    assert len(redis.eval_calls) == 1
    script, key_count, key, token = redis.eval_calls[0]
    assert "redis.call('get'" in script
    assert key_count == 1
    assert key == "ekip:ingestion:lock:55555555-5555-5555-5555-555555555555"
    assert token.startswith("job-locked:")


@pytest.mark.asyncio
async def test_final_failed_attempt_is_dead_lettered(monkeypatch) -> None:
    organization_id = __import__("uuid").uuid4()
    job_id = __import__("uuid").uuid4()
    connector_id = "66666666-6666-6666-6666-666666666666"

    class _Job:
        pass

    job = _Job()
    job.id = job_id
    job.organization_id = organization_id
    job.status = "failed"
    job.failed_stage = "fetch"

    async def fake_run_ingestion_job(session, connector_config_id):
        return job

    async def fake_dead_letter(session, passed_job_id, passed_org_id):
        assert passed_job_id == job_id
        assert passed_org_id == organization_id
        dead_lettered_job = _Job()
        dead_lettered_job.id = job_id
        dead_lettered_job.status = "dead_lettered"
        dead_lettered_job.failed_stage = "fetch"
        return dead_lettered_job

    monkeypatch.setattr(tasks_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(tasks_module.service, "run_ingestion_job", fake_run_ingestion_job)
    monkeypatch.setattr(
        tasks_module.service, "dead_letter_ingestion_job", fake_dead_letter
    )

    await tasks_module.run_ingestion_job_task(
        {"job_try": tasks_module.MAX_JOB_TRIES, "job_id": "final-attempt"},
        connector_id,
    )
