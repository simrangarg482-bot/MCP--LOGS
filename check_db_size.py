"""One-off, read-only diagnostic: report the current Neon database's total
size directly from Postgres, plus the size of its largest tables --
authoritative, no need to check the Neon web dashboard. Use this to rule
out (or confirm) hitting the free-tier storage cap again, the same issue
that caused every DB write to fail at the very start of this project's
troubleshooting.

    python check_db_size.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.database.session import async_session_factory


async def main() -> None:
    async with async_session_factory() as session:
        total = await session.execute(
            text("SELECT pg_size_pretty(pg_database_size(current_database()))")
        )
        print(f"Total database size: {total.scalar_one()}\n")

        result = await session.execute(
            text(
                """
                SELECT
                    relname AS table_name,
                    pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind = 'r'
                ORDER BY pg_total_relation_size(c.oid) DESC
                LIMIT 10
                """
            )
        )
        print("Largest tables:")
        for row in result.mappings():
            print(f"  {row['table_name']}: {row['total_size']}")


if __name__ == "__main__":
    asyncio.run(main())
