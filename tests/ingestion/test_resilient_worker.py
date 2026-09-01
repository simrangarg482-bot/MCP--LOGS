"""Regression coverage for the Redis-resilient ingestion worker entrypoint."""

from __future__ import annotations

import pytest
from arq.worker import Worker
from redis.exceptions import WatchError

from scripts.run_ingestion_worker import ResilientIngestionWorker


@pytest.mark.asyncio
async def test_transient_redis_poll_failure_is_retried(monkeypatch) -> None:
    worker = object.__new__(ResilientIngestionWorker)
    worker._redis_poll_failures = 0
    delays: list[float] = []

    async def fail_poll(_worker) -> None:
        raise WatchError("connection dropped during WATCH")

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(Worker, "_poll_iteration", fail_poll)
    monkeypatch.setattr("scripts.run_ingestion_worker.asyncio.sleep", fake_sleep)

    await worker._poll_iteration()
    await worker._poll_iteration()

    assert worker._redis_poll_failures == 2
    assert delays == [1, 2]


@pytest.mark.asyncio
async def test_successful_poll_resets_redis_backoff(monkeypatch) -> None:
    worker = object.__new__(ResilientIngestionWorker)
    worker._redis_poll_failures = 4

    async def successful_poll(_worker) -> None:
        return None

    monkeypatch.setattr(Worker, "_poll_iteration", successful_poll)

    await worker._poll_iteration()

    assert worker._redis_poll_failures == 0


@pytest.mark.asyncio
async def test_non_redis_poll_error_is_not_hidden(monkeypatch) -> None:
    worker = object.__new__(ResilientIngestionWorker)
    worker._redis_poll_failures = 0

    async def fail_poll(_worker) -> None:
        raise RuntimeError("programming bug")

    monkeypatch.setattr(Worker, "_poll_iteration", fail_poll)

    with pytest.raises(RuntimeError, match="programming bug"):
        await worker._poll_iteration()
