"""One-off, read-only diagnostic: list every connector_config row for the
organization that owns a given, known connector_config_id -- use this to
see the full, ground-truth list of connectors in one account, rather than
relying on what the UI currently shows or what we remember creating.

    python list_connectors_for_new_org.py
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import async_session_factory, set_tenant_context
from app.ingestion import repository

# A connector_config_id known to belong to the (new) account we want to
# inspect -- only used to resolve which organization to scope this read to.
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
        print(f"organization_id: {organization_id}\n")
        await set_tenant_context(session, organization_id)

        result = await session.execute(
            text(
                """
                SELECT id, status, source, config, last_synced_at, created_at
                FROM connector_configs
                WHERE organization_id = :org_id
                ORDER BY created_at
                """
            ),
            {"org_id": str(organization_id)},
        )
        rows = result.mappings().all()

        print(f"Found {len(rows)} connector_config row(s) for this org:\n")
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
