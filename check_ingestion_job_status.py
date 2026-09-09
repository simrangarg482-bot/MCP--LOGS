"""One-off diagnostic: show real-time progress of the current/latest
ingestion job for a connector, straight from Postgres -- independent of
whatever is or isn't printed to the worker's console.

Run it any time (safe, read-only) to see whether a running sync is actually
making progress:

    python check_ingestion_job_status.py
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import async_session_factory, set_tenant_context
from app.ingestion import repository

_CONNECTOR_CONFIG_ID = uuid.UUID("bdc53da6-3c9b-44f7-825c-2a451288c69f")


async def main() -> None:
    session: AsyncSession
    async with async_session_factory() as session:
        # ingestion_jobs is RLS-protected -- must resolve which org owns
        # this connector via the narrow RLS-bypass lookup and set the tenant
        # GUC before any RLS-scoped query can see its rows at all (fail
        # closed by design, see app.database.session.set_tenant_context's
        # own docstring).
        organization_id = await repository.resolve_connector_config_organization_id(
            session, _CONNECTOR_CONFIG_ID
        )
        if organization_id is None:
            print("No connector_config found with this id at all.")
            return
        await set_tenant_context(session, organization_id)

        result = await session.execute(
            text(
                """
                SELECT id, status, failed_stage, started_at, completed_at,
                       pages_fetched, items_discovered, items_skipped,
                       documents_processed, chunks_embedded, retry_count,
                       last_error_type
                FROM ingestion_jobs
                WHERE connector_config_id = :cid
                ORDER BY started_at DESC NULLS LAST
                LIMIT 5
                """
            ),
            {"cid": str(_CONNECTOR_CONFIG_ID)},
        )
        rows = result.mappings().all()

    if not rows:
        print("No ingestion_jobs rows found for this connector at all -- "
              "either RLS is blocking this read, or no job has reached the "
              "point of inserting a row yet.")
        return

    for row in rows:
        print("---")
        for key, value in row.items():
            print(f"  {key}: {value}")


if __name__ == "__main__":
    asyncio.run(main())
