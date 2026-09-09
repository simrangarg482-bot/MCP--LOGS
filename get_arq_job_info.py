"""One-off, read-only diagnostic: show the enqueued job definition (function,
args, enqueue time, score) for specific job ids -- unlike
get_arq_job_results.py, this works for jobs that are STILL in-progress or
queued (not finished yet), since the job definition is written at enqueue
time, independent of completion.

    python get_arq_job_info.py
"""

from __future__ import annotations

import asyncio

from arq import create_pool
from arq.jobs import Job

from app.shared.redis_settings import build_redis_settings

# Update this to whichever job ids check_arq_queue.py just showed you.
_JOB_IDS = [
    "ab3c8f76529641c699cf466d3ad42478",
    "e5387bb254c94a3ea71c0fb0cceb8bad",
]


async def main() -> None:
    pool = await create_pool(build_redis_settings())
    try:
        for job_id in _JOB_IDS:
            print(f"=== {job_id} ===")
            job = Job(job_id, redis=pool)
            info = await job.info()
            if info is None:
                print("  No job definition found (already expired, or never existed).")
                continue
            print(f"  function: {info.function}")
            print(f"  args: {info.args}")
            print(f"  kwargs: {info.kwargs}")
            print(f"  job_try: {info.job_try}")
            print(f"  enqueue_time: {info.enqueue_time}")
            print(f"  score: {info.score}")
    finally:
        await pool.aclose()


if __name__ == "__main__":
    asyncio.run(main())
