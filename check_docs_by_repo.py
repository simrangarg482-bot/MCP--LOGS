"""One-off, read-only diagnostic: count ingested GitHub documents grouped by
repo (parsed from each document's source_url), for this organization.

    python check_docs_by_repo.py
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import async_session_factory, set_tenant_context
from app.ingestion import repository

# Any connector_config_id belonging to the org you want to inspect -- only
# used to resolve which organization to scope this read to.
_KNOWN_CONNECTOR_CONFIG_ID = uuid.UUID("9b336f44-b10c-4b4d-b623-cb585f912621")


async def main() -> None:
    session: AsyncSession
    async with async_session_factory() as session:
        organization_id = await repository.resolve_connector_config_organization_id(
            session, _KNOWN_CONNECTOR_CONFIG_ID
        )
        if organization_id is None:
            print("Could not resolve organization from that connector id.")
            return
        await set_tenant_context(session, organization_id)

        result = await session.execute(
            text(
                """
                SELECT
                    regexp_replace(source_url, '^https://github\\.com/([^/]+/[^/]+)/.*$', '\\1')
                        AS repo,
                    count(*) AS document_count,
                    max(created_at) AS latest_ingested_at
                FROM documents
                WHERE organization_id = :org_id AND source = 'github'
                GROUP BY repo
                ORDER BY document_count DESC
                """
            ),
            {"org_id": str(organization_id)},
        )
        rows = result.mappings().all()

        if not rows:
            print("No GitHub documents found for this organization at all.")
            return

        print("Documents ingested per repo:\n")
        for row in rows:
            print(f"  {row['repo']}: {row['document_count']} documents "
                  f"(latest: {row['latest_ingested_at']})")


if __name__ == "__main__":
    asyncio.run(main())
