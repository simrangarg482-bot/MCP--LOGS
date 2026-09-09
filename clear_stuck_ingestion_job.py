"""One-off cleanup: clear the stale in-progress lock on the stuck GitHub
ingestion job so it can be retried immediately, instead of waiting out its
remaining TTL.

Deletes ONLY:
  - arq:in-progress:<job_id>  (the stale "a worker is already running this"
    lock left behind by a worker process that was killed mid-job)
  - arq:retry:<job_id>        (its retry-attempt counter, so it doesn't
    resume with an already-elevated attempt number)

Leaves untouched:
  - arq:job:<job_id>              (the job payload itself -- still queued)
  - its entry in arq:queue:ingestion (still queued, unchanged score)
  - every other key in Redis
  - all Postgres data

Run once:

    python clear_stuck_ingestion_job.py
"""

from __future__ import annotations

import asyncio

from app.shared.redis_settings import build_redis_settings
from arq.connections import ArqRedis, create_pool

_JOB_ID = "e3233755252f42ad858d1a3030936a80"
_KEYS_TO_DELETE = (
    f"arq:in-progress:{_JOB_ID}",
    f"arq:retry:{_JOB_ID}",
)


async def main() -> None:
    pool: ArqRedis = await create_pool(build_redis_settings())

    print("About to delete:")
    for key in _KEYS_TO_DELETE:
        exists = await pool.exists(key)
        print(f"  {key}  (exists={bool(exists)})")

    confirm = input("\nType 'yes' to delete these keys: ").strip().lower()
    if confirm != "yes":
        print("Aborted -- nothing deleted.")
        await pool.aclose()
        return

    deleted = await pool.delete(*_KEYS_TO_DELETE)
    print(f"\nDeleted {deleted} key(s).")

    # Confirm the job is still queued and ready to be picked up.
    still_queued = await pool.zscore("arq:queue:ingestion", _JOB_ID)
    print(f"Job {_JOB_ID} still in arq:queue:ingestion: {still_queued is not None} "
          f"(score={still_queued})")

    await pool.aclose()


if __name__ == "__main__":
    asyncio.run(main())
