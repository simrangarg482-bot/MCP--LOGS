"""One-off, read-only diagnostic: does ANY organization, anywhere, have a
connector_configs row with source in ('incidents', 'runbooks')? These are
the two internal, self-ingesting connectors (re-ingest this app's own
incidents/postmortems tables into their retrieval collections) -- if none
exist anywhere, it confirms there is currently no path, product-wide, to
populate the "incidents" retrieval collection that search_similar_incidents
depends on.

    python check_internal_connectors.py
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
            SELECT id, organization_id, source, status, created_at
            FROM connector_configs
            WHERE source IN ('incidents', 'runbooks')
            ORDER BY created_at
            """
        )
        print(f"Found {len(rows)} connector_configs row(s) with source in (incidents, runbooks), across ALL orgs:\n")
        for r in rows:
            print(f"  id={r['id']} org={r['organization_id']} source={r['source']} status={r['status']} created_at={r['created_at']}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
