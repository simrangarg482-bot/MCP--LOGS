"""One-off, read-only diagnostic: fetch arq's own recorded result (success/
exception, return value, timing) for specific job ids directly from Redis,
using arq's own Job API so results are deserialized correctly.

Use this when a job has an `arq:result:<job_id>` key in Redis (per
check_arq_queue.py) but no corresponding row shows up in ingestion_jobs --
this tells you whether the job crashed before it ever got to write to
Postgres, and if so, with what exception.

    python get_arq_job_results.py
"""

from __future__ import annotations

import asyncio

from arq import create_pool
from arq.jobs import Job

from app.shared.redis_settings import build_redis_settings

# The newest job ids to inspect -- update this list to whichever
# arq:result:* keys check_arq_queue.py just showed you that you haven't
# seen before.
_JOB_IDS = [
    "1753dcf3f6884886a2560c028bd69829",
    "59b9050343eb4f5f96f969db4c9786eb",
    "af37d6e1ac24461c9fb2e6122bcf2455",
    "d39b88275344433eb128e990cb7dc5bb",
    "d54404bb937e49b1a1058832ef12b313",
]


async def main() -> None:
    pool = await create_pool(build_redis_settings())

    try:
        for job_id in _JOB_IDS:
            print(f"=== {job_id} ===")
            job = Job(job_id, redis=pool)
            info = await job.result_info()
            if info is None:
                print("  No result recorded for this job id (not finished, or expired).")
                continue
            print(f"  function: {info.function}")
            print(f"  args: {info.args}")
            print(f"  kwargs: {info.kwargs}")
            print(f"  job_try: {info.job_try}")
            print(f"  enqueue_time: {info.enqueue_time}")
            print(f"  start_time: {info.start_time}")
            print(f"  finish_time: {info.finish_time}")
            print(f"  success: {info.success}")
            print(f"  result: {info.result!r}")
    finally:
        await pool.aclose()


if __name__ == "__main__":
    asyncio.run(main())
