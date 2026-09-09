"""One-off, read-only diagnostic: list every `ekip:ingestion:lock:*` key in
Redis and its remaining TTL. These are the per-connector locks
`_acquire_connector_lock` (app/ingestion/workers/tasks.py) sets before a
sync runs; if a worker process dies mid-job without releasing one, every
subsequent sync-now for that same connector silently no-ops
("ingestion_job_task_skipped_duplicate") until the lock's TTL expires on
its own.

    python check_connector_locks.py
"""

from __future__ import annotations

import asyncio

from arq import create_pool

from app.shared.redis_settings import build_redis_settings

_LOCK_PREFIX = "ekip:ingestion:lock:"


async def main() -> None:
    pool = await create_pool(build_redis_settings())
    try:
        keys = [key async for key in pool.scan_iter(match=f"{_LOCK_PREFIX}*")]
        if not keys:
            print("No ekip:ingestion:lock:* keys found -- nothing is currently locked.")
            return

        print(f"Found {len(keys)} connector lock key(s):\n")
        for key in keys:
            key_str = key.decode() if isinstance(key, bytes) else key
            connector_config_id = key_str[len(_LOCK_PREFIX):]
            ttl = await pool.ttl(key)
            value = await pool.get(key)
            value_str = value.decode() if isinstance(value, bytes) else value
            print(f"  connector_config_id: {connector_config_id}")
            print(f"    ttl_seconds: {ttl}  (~{ttl / 60:.1f} min remaining)")
            print(f"    lock_token: {value_str}")
    finally:
        await pool.aclose()


if __name__ == "__main__":
    asyncio.run(main())
