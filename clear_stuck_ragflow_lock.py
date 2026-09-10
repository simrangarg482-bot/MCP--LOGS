"""Deletes the Redis keys for the stuck ragflow (connector 8ce5209a) job so
a fresh sync-now / worker restart can claim it immediately. Requires typed
'yes' confirmation. Does not touch Postgres or arq:job:* (job history).

    python clear_stuck_ragflow_lock.py
"""

from __future__ import annotations

import asyncio

from arq import create_pool

from app.shared.redis_settings import build_redis_settings

_JOB_ID = "e867c42089cf4ec7a1bd1d73e1744f24"
_CONNECTOR_CONFIG_ID = "8ce5209a-59a0-4a2c-9621-5f0238e62b73"

_KEYS = [
    f"arq:in-progress:{_JOB_ID}",
    f"arq:retry:{_JOB_ID}",
    f"ekip:ingestion:lock:{_CONNECTOR_CONFIG_ID}",
]


async def main() -> None:
    pool = await create_pool(build_redis_settings())
    try:
        existing = []
        for key in _KEYS:
            if await pool.exists(key):
                existing.append(key)

        if not existing:
            print("None of these keys currently exist -- nothing to clear.")
            return

        print("About to delete these keys:")
        for key in existing:
            print(f"  {key}")
        print(
            "\nThis only releases the stuck lock/in-progress markers for the "
            "current ragflow (8ce5209a) job so a fresh worker restart can "
            "claim it immediately -- it does not touch arq:job:* (job "
            "payload/history) or any Postgres row. The stale 'running' "
            "ingestion_jobs row will auto-close as failed/worker_interrupted "
            "the moment the next attempt acquires the lock, same as before."
        )
        confirm = input("\nType 'yes' to proceed: ").strip().lower()
        if confirm != "yes":
            print("Aborted, nothing deleted.")
            return

        deleted = 0
        for key in existing:
            deleted += await pool.delete(key)
        print(f"\nDeleted {deleted} key(s). Ragflow (8ce5209a) is free to sync fresh now.")
    finally:
        await pool.aclose()


if __name__ == "__main__":
    asyncio.run(main())
