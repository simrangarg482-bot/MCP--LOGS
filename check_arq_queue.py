"""One-off diagnostic: inspect the arq ingestion queue directly in Redis.

Run this, then click "Sync Now" in another window, then run it again
immediately after -- comparing the two outputs tells us whether the job is
actually landing in Redis under the key the ingestion worker listens on.

    python check_arq_queue.py
"""

from __future__ import annotations

import asyncio

from app.shared.config.settings import get_settings
from app.shared.redis_settings import build_redis_settings
from arq.connections import ArqRedis, create_pool

_QUEUE_NAME = "arq:queue:ingestion"


async def main() -> None:
    settings = get_settings()
    print(f"Redis: {settings.redis_url.host}:{settings.redis_url.port} "
          f"db={settings.redis_url.path or '/0'}")

    pool: ArqRedis = await create_pool(build_redis_settings())

    all_keys = await pool.keys("arq:*")
    print(f"\nAll arq:* keys in this Redis ({len(all_keys)} total):")
    for k in sorted(all_keys):
        key = k.decode() if isinstance(k, bytes) else k
        key_type = await pool.type(k)
        key_type = key_type.decode() if isinstance(key_type, bytes) else key_type
        print(f"  {key}  ({key_type})")

    print(f"\nQueue '{_QUEUE_NAME}':")
    members = await pool.zrange(_QUEUE_NAME, 0, -1, withscores=True)
    if not members:
        print("  EMPTY -- no job is currently queued under this name.")
    else:
        for job_id, score in members:
            jid = job_id.decode() if isinstance(job_id, bytes) else job_id
            print(f"  job_id={jid}  score={score}")

    # Default arq queue name too, in case something is enqueuing there
    # instead (e.g. a stale pool without default_queue_name set).
    default_members = await pool.zrange("arq:queue", 0, -1, withscores=True)
    print("\nDefault queue 'arq:queue' (arq's built-in default, should be "
          "unused by this project):")
    if not default_members:
        print("  EMPTY")
    else:
        for job_id, score in default_members:
            jid = job_id.decode() if isinstance(job_id, bytes) else job_id
            print(f"  job_id={jid}  score={score}")

    # Diagnose whether a specific job's in-progress lock is stale -- a
    # non-negative TTL means the lock is still "live" (a worker may
    # genuinely be about to pick it up on its next retry), while -2 means
    # the key doesn't exist at all (never locked, or already expired).
    print("\nPer-job in-progress / retry lock details:")
    job_ids = set()
    for k in all_keys:
        key = k.decode() if isinstance(k, bytes) else k
        for prefix in ("arq:in-progress:", "arq:job:", "arq:retry:"):
            if key.startswith(prefix):
                job_ids.add(key[len(prefix):])
    for jid in sorted(job_ids):
        for prefix in ("arq:in-progress:", "arq:job:", "arq:retry:"):
            key = f"{prefix}{jid}"
            exists = await pool.exists(key)
            if exists:
                ttl = await pool.ttl(key)
                print(f"  {key}  ttl={ttl}s")

    await pool.aclose()


if __name__ == "__main__":
    asyncio.run(main())
