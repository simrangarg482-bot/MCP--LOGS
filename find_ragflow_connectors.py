"""One-off, read-only diagnostic: find every connector_configs row across
ALL organizations whose config mentions "ragflow", using the migration/
superuser DB connection (bypasses RLS) so we're not scoped to one org.

    python find_ragflow_connectors.py
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
            SELECT id, organization_id, project_id, status, config,
                   last_synced_at, created_at, updated_at
            FROM connector_configs
            WHERE source = 'github' AND config::text ILIKE '%ragflow%'
            ORDER BY created_at
            """
        )
        print(f"Found {len(rows)} connector_configs row(s) mentioning ragflow (any org):\n")
        for r in rows:
            print("---")
            print(f"  id: {r['id']}")
            print(f"  organization_id: {r['organization_id']}")
            print(f"  project_id: {r['project_id']}")
            print(f"  status: {r['status']}")
            print(f"  config: {r['config']}")
            print(f"  last_synced_at: {r['last_synced_at']}")
            print(f"  created_at: {r['created_at']}")
            print(f"  updated_at: {r['updated_at']}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
