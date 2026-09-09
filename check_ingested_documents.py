"""One-off, read-only diagnostic: does this connector actually have real
documents/chunks in the database already (proof ingestion worked at some
point), independent of what the current in-flight job's own counters say.

    python check_ingested_documents.py
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import async_session_factory, set_tenant_context
from app.ingestion import repository

_CONNECTOR_CONFIG_ID = uuid.UUID("22c3a878-8eb9-45e8-bc6a-db1175ad1654")


async def main() -> None:
    session: AsyncSession
    async with async_session_factory() as session:
        organization_id = await repository.resolve_connector_config_organization_id(
            session, _CONNECTOR_CONFIG_ID
        )
        if organization_id is None:
            print("No connector_config found with this id.")
            return
        await set_tenant_context(session, organization_id)

        doc_count = await session.execute(
            text(
                """
                SELECT count(*), max(created_at)
                FROM documents
                WHERE organization_id = :org_id AND source = 'github'
                """
            ),
            {"org_id": str(organization_id)},
        )
        row = doc_count.one()
        print(f"documents (source=github): count={row[0]}  latest_created_at={row[1]}")

        chunk_count = await session.execute(
            text(
                """
                SELECT count(*)
                FROM code_chunks
                WHERE organization_id = :org_id
                """
            ),
            {"org_id": str(organization_id)},
        )
        print(f"code_chunks: count={chunk_count.scalar_one()}")

        sample = await session.execute(
            text(
                """
                SELECT title, source_url, version, created_at
                FROM documents
                WHERE organization_id = :org_id AND source = 'github'
                ORDER BY created_at DESC
                LIMIT 5
                """
            ),
            {"org_id": str(organization_id)},
        )
        print("\nMost recent 5 documents:")
        for r in sample.mappings():
            print(f"  {r['created_at']}  v{r['version']}  {r['title']}  ({r['source_url']})")


if __name__ == "__main__":
    asyncio.run(main())
