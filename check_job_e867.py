"""One-off, read-only diagnostic: check whether arq itself still considers
job e867c42089cf4ec7a1bd1d73e1744f24 (the current ragflow/8ce5209a attempt)
in-progress, finished, or vanished -- to tell apart a genuinely still-running
job from one that silently died without ever reaching the failure-handling
code path (which would explain the ingestion_jobs row never flipping to
failed).

    python check_job_e867.py
"""

from __future__ import annotations

import asyncio

from arq import create_pool
from arq.jobs import Job

from app.shared.redis_settings import build_redis_settings

_JOB_ID = "e867c42089cf4ec7a1bd1d73e1744f24"


async def main() -> None:
    pool = await create_pool(build_redis_settings())
    try:
        in_progress_key = f"arq:in-progress:{_JOB_ID}"
        job_key = f"arq:job:{_JOB_ID}"
        result_key = f"arq:result:{_JOB_ID}"

        for key in (in_progress_key, job_key, result_key):
            exists = await pool.exists(key)
            ttl = await pool.ttl(key) if exists else None
            print(f"{key}: exists={bool(exists)} ttl={ttl}")

        job = Job(_JOB_ID, redis=pool)
        info = await job.info()
        print(f"\njob.info(): {info}")
        result_info = await job.result_info()
        print(f"job.result_info(): {result_info}")
    finally:
        await pool.aclose()


if __name__ == "__main__":
    asyncio.run(main())
