"""One-off, read-only diagnostic: current connector_configs state + the 5
most recent ingestion_jobs rows for the AI-Job-Application-Agent connector
specifically, to see whether a sync-now click actually created a new job
row (vs. never reaching the queue at all, or a job that ran and finished
so long ago its arq result key already expired).

    python check_ai_job_connector.py
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import async_session_factory, set_tenant_context
from app.ingestion import repository

_CONNECTOR_CONFIG_ID = uuid.UUID("9b336f44-b10c-4b4d-b623-cb585f912621")


async def main() -> None:
    session: AsyncSession
    async with async_session_factory() as session:
        organization_id = await repository.resolve_connector_config_organization_id(
            session, _CONNECTOR_CONFIG_ID
        )
        if organization_id is None:
            print("Could not resolve organization -- connector id may be wrong.")
            return
        await set_tenant_context(session, organization_id)

        row = (
            await session.execute(
                text(
                    "SELECT status, last_synced_at, config, updated_at "
                    "FROM connector_configs WHERE id = :cid"
                ),
                {"cid": str(_CONNECTOR_CONFIG_ID)},
            )
        ).mappings().one()
        print("connector_configs row:")
        print(f"  status: {row['status']}")
        print(f"  last_synced_at: {row['last_synced_at']}")
        print(f"  updated_at: {row['updated_at']}")
        print(f"  config: {row['config']}")

        jobs = (
            await session.execute(
                text(
                    """
                    SELECT id, status, started_at, completed_at, failed_stage,
                           last_error_type, items_discovered, items_skipped,
                           documents_processed, pages_fetched, retry_count,
                           created_at
                    FROM ingestion_jobs
                    WHERE connector_config_id = :cid
                    ORDER BY created_at DESC
                    LIMIT 5
                    """
                ),
                {"cid": str(_CONNECTOR_CONFIG_ID)},
            )
        ).mappings().all()

        print("\nMost recent ingestion_jobs rows:")
        for j in jobs:
            print(
                f"  id={j['id']} status={j['status']} created={j['created_at']} "
                f"started={j['started_at']} completed={j['completed_at']} "
                f"discovered={j['items_discovered']} skipped={j['items_skipped']} "
                f"processed={j['documents_processed']} pages={j['pages_fetched']} "
                f"retries={j['retry_count']} failed_stage={j['failed_stage']} "
                f"last_error_type={j['last_error_type']}"
            )


if __name__ == "__main__":
    asyncio.run(main())
