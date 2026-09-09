"""One-off, read-only diagnostic: list every GitHub connector_config row for
this organization, so we can see duplicate registrations side by side.

    python list_github_connectors.py
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import async_session_factory, set_tenant_context
from app.ingestion import repository

# Either of the two connector_config_ids we've seen so far -- used only to
# resolve which organization to scope this read to.
_KNOWN_CONNECTOR_CONFIG_ID = uuid.UUID("9b21a991-1de5-4253-9fbf-a80c992f3026")


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
                SELECT id, status, source, config, last_synced_at, created_at
                FROM connector_configs
                WHERE organization_id = :org_id AND source = 'github'
                ORDER BY created_at
                """
            ),
            {"org_id": str(organization_id)},
        )
        rows = result.mappings().all()

        print(f"Found {len(rows)} github connector_configs row(s) for this org:\n")
        for row in rows:
            print("---")
            for key, value in row.items():
                print(f"  {key}: {value}")

            job_count = await session.execute(
                text("SELECT count(*) FROM ingestion_jobs WHERE connector_config_id = :cid"),
                {"cid": str(row["id"])},
            )
            print(f"  ingestion_jobs for this connector: {job_count.scalar_one()}")


if __name__ == "__main__":
    asyncio.run(main())
