"""Interactive: clear the stalled lock on the EKIP-repo connector
(1ce23965-...) so a fresh sync-now can run immediately instead of waiting
for its lock's TTL to expire naturally (~15-20 more minutes at time of
writing).

Deletes exactly 3 keys, all tied to the one confirmed-stalled job
(arq job e5387bb254c94a3ea71c0fb0cceb8bad, ingestion_jobs row
1a03f540-8840-45fa-88b1-1e9a7c41c21a -- zero progress recorded, checkpoint
frozen since the previous day):
  - arq:in-progress:e5387bb254c94a3ea71c0fb0cceb8bad  (arq's own claim marker)
  - arq:retry:e5387bb254c94a3ea71c0fb0cceb8bad          (arq's retry counter, if present)
  - ekip:ingestion:lock:1ce23965-147d-49b7-877c-20bd424e69bd  (this project's
    own per-connector lock -- the one actually blocking a new run via
    `_acquire_connector_lock` in app/ingestion/workers/tasks.py)

Does NOT touch arq:job:<id> (the job payload/history) or any row in
Postgres. The next time a job successfully acquires the connector lock,
`insert_ingestion_job` (app/ingestion/repository.py) automatically marks
the stale `running` ingestion_jobs row as `failed`/`worker_interrupted` in
the same transaction -- so no manual DB write is needed here either.

Requires typed confirmation before deleting anything.

    python clear_stuck_ekip_repo_lock.py
"""

from __future__ import annotations

import asyncio

from arq import create_pool

from app.shared.redis_settings import build_redis_settings

_ARQ_JOB_ID = "e5387bb254c94a3ea71c0fb0cceb8bad"
_CONNECTOR_CONFIG_ID = "1ce23965-147d-49b7-877c-20bd424e69bd"

_KEYS = [
    f"arq:in-progress:{_ARQ_JOB_ID}",
    f"arq:retry:{_ARQ_JOB_ID}",
    f"ekip:ingestion:lock:{_CONNECTOR_CONFIG_ID}",
]


async def main() -> None:
    pool = await create_pool(build_redis_settings())
    try:
        keys_to_delete = [k for k in _KEYS if await pool.exists(k)]

        if not keys_to_delete:
            print("Nothing to clear -- none of these keys currently exist (may have already expired).")
            return

        print("About to delete these keys:")
        for key in keys_to_delete:
            print(f"  {key}")
        print(
            "\nThis only releases the stalled lock on the EKIP-repo connector so a "
            "fresh sync-now can run -- it does not touch arq:job:* (job payload/"
            "history) or any Postgres row. The stalled ingestion_jobs row will "
            "be auto-marked failed/worker_interrupted as soon as the next job "
            "for this connector starts."
        )
        confirmation = input("\nType 'yes' to proceed: ").strip()
        if confirmation != "yes":
            print("Aborted -- nothing deleted.")
            return

        deleted = await pool.delete(*keys_to_delete)
        print(f"\nDeleted {deleted} key(s). You can click sync-now on the EKIP-repo connector again now.")
    finally:
        await pool.aclose()


if __name__ == "__main__":
    asyncio.run(main())
