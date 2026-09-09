"""Interactive: clear the orphaned mcp-logs ingestion lock so a fresh
sync-now can run immediately instead of waiting ~50 more minutes for the
lock's TTL to expire naturally.

Deletes exactly 3 keys, all tied to the one confirmed-orphaned job
(arq job e23f6a7a..., ingestion_jobs row f1a13222...):
  - arq:in-progress:e23f6a7a50d2469b99d6dea18a08f1ea  (arq's own claim marker)
  - arq:retry:e23f6a7a50d2469b99d6dea18a08f1ea          (arq's retry counter, if present)
  - ekip:ingestion:lock:9b336f44-b10c-4b4d-b623-cb585f912621  (this project's
    own per-connector lock -- the one actually blocking a new run via
    `_acquire_connector_lock` in app/ingestion/workers/tasks.py)

Does NOT touch arq:job:<id> (the job payload/history) or any row in
Postgres. The next time a job successfully acquires the connector lock,
`insert_ingestion_job` (app/ingestion/repository.py) automatically marks
the stale `running` ingestion_jobs row as `failed`/`worker_interrupted` in
the same transaction -- so no manual DB write is needed here either.

Requires typed confirmation before deleting anything.

    python clear_stuck_mcp_logs_lock.py
"""

from __future__ import annotations

import asyncio

from arq import create_pool

from app.shared.redis_settings import build_redis_settings

_ARQ_JOB_ID = "e23f6a7a50d2469b99d6dea18a08f1ea"
_CONNECTOR_CONFIG_ID = "9b336f44-b10c-4b4d-b623-cb585f912621"

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
            "\nThis only releases the stale lock on the mcp-logs connector so a "
            "fresh sync-now can run -- it does not touch arq:job:* (job payload/"
            "history) or any Postgres row. The orphaned ingestion_jobs row will "
            "be auto-marked failed/worker_interrupted as soon as the next job "
            "for this connector starts."
        )
        confirmation = input("\nType 'yes' to proceed: ").strip()
        if confirmation != "yes":
            print("Aborted -- nothing deleted.")
            return

        deleted = await pool.delete(*keys_to_delete)
        print(f"\nDeleted {deleted} key(s). You can click sync-now on mcp-logs again now.")
    finally:
        await pool.aclose()


if __name__ == "__main__":
    asyncio.run(main())
