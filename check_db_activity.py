"""One-off, read-only diagnostic: show what Postgres itself sees every
active connection/session doing right now (state, wait_event, how long the
current query has been running) -- to see directly whether the stalled
ingestion worker is blocked on a lock, waiting for a connection, or
genuinely idle.

    python check_db_activity.py
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()


async def main() -> None:
    dsn = os.environ.get("MIGRATION_DATABASE_URL") or os.environ["DATABASE_URL"]
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://").replace("postgres+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT pid, usename, application_name, state,
                   wait_event_type, wait_event,
                   now() - query_start AS query_age,
                   now() - xact_start AS xact_age,
                   now() - state_change AS state_age,
                   left(query, 150) AS query_snippet
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND pid <> pg_backend_pid()
            ORDER BY query_start ASC NULLS LAST
            """
        )
        print(f"{len(rows)} other connection(s) to this database:\n")
        for r in rows:
            print("---")
            print(f"  pid: {r['pid']}  app: {r['application_name']}  state: {r['state']}")
            print(f"  wait_event_type: {r['wait_event_type']}  wait_event: {r['wait_event']}")
            print(f"  query_age: {r['query_age']}  xact_age: {r['xact_age']}  state_age: {r['state_age']}")
            print(f"  query: {r['query_snippet']}")

        print("\n=== blocking locks (if any) ===")
        blocks = await conn.fetch(
            """
            SELECT blocked_locks.pid AS blocked_pid,
                   blocking_locks.pid AS blocking_pid,
                   blocked_activity.query AS blocked_query,
                   blocking_activity.query AS blocking_query
            FROM pg_catalog.pg_locks blocked_locks
            JOIN pg_catalog.pg_stat_activity blocked_activity
                ON blocked_activity.pid = blocked_locks.pid
            JOIN pg_catalog.pg_locks blocking_locks
                ON blocking_locks.locktype = blocked_locks.locktype
                AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
                AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
                AND blocking_locks.page IS NOT DISTINCT FROM blocked_locks.page
                AND blocking_locks.tuple IS NOT DISTINCT FROM blocked_locks.tuple
                AND blocking_locks.transactionid IS NOT DISTINCT FROM blocked_locks.transactionid
                AND blocking_locks.pid != blocked_locks.pid
            JOIN pg_catalog.pg_stat_activity blocking_activity
                ON blocking_activity.pid = blocking_locks.pid
            WHERE NOT blocked_locks.granted
            """
        )
        if not blocks:
            print("  none")
        for b in blocks:
            print(f"  pid {b['blocked_pid']} blocked by pid {b['blocking_pid']}")
            print(f"    blocked query: {b['blocked_query'][:150]}")
            print(f"    blocking query: {b['blocking_query'][:150]}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
