"""One-off diagnostic: enqueue a real run_ingestion_job_task job directly,
using a freshly-created arq pool (the exact same construction the API's
lifespan uses), completely bypassing the FastAPI endpoint and frontend.

This isolates whether the problem is in the long-lived `app.state.arq_pool`
object the running API process holds (e.g. a connection that silently
stopped working after some earlier retry/reconnect), or something deeper.

Prints the exact job_id enqueue_job() returns, then immediately re-checks
Redis for it -- so we see, within this one script, whether a fresh enqueue
call's own return value can be trusted.

    python test_direct_enqueue.py
"""

from __future__ import annotations

import asyncio
import uuid

from arq import create_pool
from arq.jobs import Job

from app.shared.redis_settings import build_redis_settings

_CONNECTOR_CONFIG_ID = "9b336f44-b10c-4b4d-b623-cb585f912621"


async def main() -> None:
    pool = await create_pool(
        build_redis_settings(), default_queue_name="arq:queue:ingestion"
    )
    try:
        job = await pool.enqueue_job("run_ingestion_job_task", _CONNECTOR_CONFIG_ID)
        if job is None:
            print("enqueue_job returned None -- arq considered this a duplicate/no-op.")
            return
        print(f"enqueue_job returned job_id: {job.job_id}")

        # Immediately check whether it actually landed in Redis.
        raw_job_key = await pool.exists(f"arq:job:{job.job_id}")
        in_queue = await pool.zscore("arq:queue:ingestion", job.job_id)
        print(f"arq:job:{job.job_id} exists in Redis: {bool(raw_job_key)}")
        print(f"score in arq:queue:ingestion zset: {in_queue}")

        print("\nWaiting up to 45s for the worker to pick it up and finish...")
        for _ in range(45):
            await asyncio.sleep(1)
            info = await Job(job.job_id, redis=pool).result_info()
            if info is not None:
                print(f"\nFinished. success={info.success} result={info.result!r}")
                break
        else:
            print("\nStill no result after 45s -- check check_arq_queue.py now.")
    finally:
        await pool.aclose()


if __name__ == "__main__":
    asyncio.run(main())
