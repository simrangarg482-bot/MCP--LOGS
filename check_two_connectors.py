"""One-off, read-only diagnostic: show connector_config status + latest
ingestion_jobs rows for a small, explicit list of connector_config_ids --
useful right after a sync-now click when you already know the id(s)
involved (e.g. from get_arq_job_results.py) and just want the DB-level
truth for exactly those, regardless of which organization they belong to.

    python check_two_connectors.py
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import async_session_factory, set_tenant_context
from app.ingestion import repository

_CONNECTOR_CONFIG_IDS = [
    uuid.UUID("17671976-0869-45b7-9d2a-bf50b87f6aba"),
    uuid.UUID("9b336f44-b10c-4b4d-b623-cb585f912621"),
]


async def main() -> None:
    session: AsyncSession
    async with async_session_factory() as session:
        for connector_config_id in _CONNECTOR_CONFIG_IDS:
            print(f"=== connector_config_id: {connector_config_id} ===")
            organization_id = await repository.resolve_connector_config_organization_id(
                session, connector_config_id
            )
            if organization_id is None:
                print("  No connector_config found with this id at all.")
                continue
            await set_tenant_context(session, organization_id)

            config_row = await session.execute(
                text(
                    """
                    SELECT status, source, config, last_synced_at, created_at
                    FROM connector_configs
                    WHERE id = :cid
                    """
                ),
                {"cid": str(connector_config_id)},
            )
            row = config_row.mappings().one_or_none()
            if row is None:
                print("  connector_config row not visible under this org's RLS context.")
            else:
                print(f"  status: {row['status']}")
                print(f"  source: {row['source']}")
                print(f"  config: {row['config']}")
                print(f"  last_synced_at: {row['last_synced_at']}")
                print(f"  created_at: {row['created_at']}")

            jobs = await session.execute(
                text(
                    """
                    SELECT id, status, failed_stage, last_error_type,
                           started_at, completed_at, documents_processed,
                           chunks_embedded, retry_count
                    FROM ingestion_jobs
                    WHERE connector_config_id = :cid
                    ORDER BY started_at DESC NULLS LAST
                    LIMIT 3
                    """
                ),
                {"cid": str(connector_config_id)},
            )
            job_rows = jobs.mappings().all()
            if not job_rows:
                print("  No ingestion_jobs rows for this connector at all.")
            for j in job_rows:
                print("  ---")
                for key, value in j.items():
                    print(f"    {key}: {value}")

            lock_key = f"ekip:ingestion:lock:{connector_config_id}"
            print(f"  (check_connector_locks.py will show whether {lock_key} is currently held)")
            print()


if __name__ == "__main__":
    asyncio.run(main())
