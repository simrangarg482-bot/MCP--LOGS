"""Interactive: clear specific orphaned `ekip:ingestion:lock:*`-style arq
`in-progress`/`retry` keys, left behind when the worker process holding
them was killed (e.g. by a restart) before it could release them.

Deletes ONLY the in-progress and retry keys for the given job ids -- the
job payload itself (`arq:job:<job_id>`) and its position in the queue
zset are left untouched, so a running worker will pick the job up fresh
right after this runs (any already-committed documents from a prior
partial attempt are safe either way; the ingestion pipeline commits per
item, not per job).

Requires typed confirmation before deleting anything.

    python clear_stuck_locks.py
"""

from __future__ import annotations

import asyncio

from arq import create_pool

from app.shared.redis_settings import build_redis_settings

# Update this to whichever job ids check_arq_queue.py showed as
# in-progress with no corresponding worker actually running them.
_JOB_IDS = [
    "5af525c91c7540a7b3854306372f609c",
    "f7ccac5ae89c4b7ba00fc301272b1485",
]


async def main() -> None:
    pool = await create_pool(build_redis_settings())
    try:
        keys_to_delete: list[str] = []
        for job_id in _JOB_IDS:
            for prefix in ("arq:in-progress:", "arq:retry:"):
                key = f"{prefix}{job_id}"
                if await pool.exists(key):
                    keys_to_delete.append(key)

        if not keys_to_delete:
            print("Nothing to clear -- none of these lock/retry keys currently exist.")
            return

        print("About to delete these keys:")
        for key in keys_to_delete:
            print(f"  {key}")
        print(
            "\nThis only releases the lock so a running worker can pick these jobs "
            "up fresh -- it does not touch arq:job:* (the job payload) or any "
            "already-committed documents."
        )
        confirmation = input("\nType 'yes' to proceed: ").strip()
        if confirmation != "yes":
            print("Aborted -- nothing deleted.")
            return

        deleted = await pool.delete(*keys_to_delete)
        print(f"\nDeleted {deleted} key(s). The worker should pick these jobs up shortly.")
    finally:
        await pool.aclose()


if __name__ == "__main__":
    asyncio.run(main())
