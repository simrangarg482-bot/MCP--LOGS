"""Run the ingestion ARQ worker with transient Redis poll recovery.

The stock ``arq`` CLI lets a ``redis.exceptions.WatchError`` raised inside
``Worker.start_jobs`` escape its main polling loop, terminating the whole OS
process. Command-level Redis retries cannot replay a lost WATCH transaction,
so this worker catches only Redis connectivity/pipeline failures at the poll
boundary, backs off, and lets redis-py establish a fresh connection on the
next iteration. Running ingestion jobs remain owned by the same Worker and
continue normally; application exceptions still propagate through ARQ's
normal job retry/dead-letter behavior.

Local/production command::

    python scripts/run_ingestion_worker.py
"""

from __future__ import annotations

import asyncio

from arq.worker import Worker, get_kwargs
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.exceptions import WatchError

from app.ingestion.workers.main import WorkerSettings
from app.shared.config.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)

_MAX_REDIS_POLL_BACKOFF_SECONDS = 30.0
_TRANSIENT_REDIS_ERRORS = (RedisConnectionError, RedisTimeoutError, WatchError)


class ResilientIngestionWorker(Worker):
    """ARQ worker that survives transient Redis failures between jobs."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._redis_poll_failures = 0

    async def _poll_iteration(self) -> None:
        try:
            await super()._poll_iteration()
        except _TRANSIENT_REDIS_ERRORS as exc:
            self._redis_poll_failures += 1
            delay = min(2 ** (self._redis_poll_failures - 1), _MAX_REDIS_POLL_BACKOFF_SECONDS)
            logger.warning(
                "ingestion_worker_redis_poll_retry",
                attempt=self._redis_poll_failures,
                delay_seconds=delay,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            await asyncio.sleep(delay)
        else:
            self._redis_poll_failures = 0


def main() -> None:
    ResilientIngestionWorker(**get_kwargs(WorkerSettings)).run()


if __name__ == "__main__":
    main()
