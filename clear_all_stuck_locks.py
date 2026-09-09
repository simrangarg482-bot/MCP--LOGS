"""Interactive: clear the 4 confirmed-orphaned locks left over after a
forced process kill (Stop-Process -Force doesn't touch Redis, so these
locks kept counting down unaffected -- their owning processes are gone,
but the locks remain until TTL expiry unless cleared here).

Connector -> arq job id:
  1ce23965-147d-49b7-877c-20bd424e69bd (EKIP repo)         -> e5387bb254c94a3ea71c0fb0cceb8bad
  06a55441-68b2-4014-84f4-a3d6bccf2c0f (ragflow)            -> ab3c8f76529641c699cf466d3ad42478
  61e88360-3de9-489b-b861-d1a6040d5356                      -> 56634cd65531423cb9d4e8545786d780
  2896312f-1275-4e2b-a022-daccc814017a                      -> 59b9050343eb4f5f96f969db4c9786eb

Deletes only arq:in-progress:*, arq:retry:*, and ekip:ingestion:lock:* for
these 4 -- never arq:job:* (payload/history) or any Postgres row. The next
attempt for each connector will auto-mark its stale `running` row as
failed/worker_interrupted (app/ingestion/repository.py's insert_ingestion_job),
same as every other time this session.

Requires typed confirmation before deleting anything.

    python clear_all_stuck_locks.py
"""

from __future__ import annotations

import asyncio

from arq import create_pool

from app.shared.redis_settings import build_redis_settings

_PAIRS = [
    ("1ce23965-147d-49b7-877c-20bd424e69bd", "e5387bb254c94a3ea71c0fb0cceb8bad"),
    ("06a55441-68b2-4014-84f4-a3d6bccf2c0f", "ab3c8f76529641c699cf466d3ad42478"),
    ("61e88360-3de9-489b-b861-d1a6040d5356", "56634cd65531423cb9d4e8545786d780"),
    ("2896312f-1275-4e2b-a022-daccc814017a", "59b9050343eb4f5f96f969db4c9786eb"),
]

_KEYS = []
for connector_id, job_id in _PAIRS:
    _KEYS.append(f"arq:in-progress:{job_id}")
    _KEYS.append(f"arq:retry:{job_id}")
    _KEYS.append(f"ekip:ingestion:lock:{connector_id}")


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
            "\nThis only releases these 4 stale locks so fresh sync-now/reconciliation "
            "attempts can claim them immediately -- it does not touch arq:job:* (job "
            "payload/history) or any Postgres row."
        )
        confirmation = input("\nType 'yes' to proceed: ").strip()
        if confirmation != "yes":
            print("Aborted -- nothing deleted.")
            return

        deleted = await pool.delete(*keys_to_delete)
        print(f"\nDeleted {deleted} key(s). These 4 connectors are free to sync fresh now.")
    finally:
        await pool.aclose()


if __name__ == "__main__":
    asyncio.run(main())
