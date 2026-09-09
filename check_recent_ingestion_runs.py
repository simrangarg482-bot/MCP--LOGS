"""One-off, read-only diagnostic: show the most recent ingestion_jobs rows
across every connector for this organization, newest first -- so you don't
need to know a specific connector_config_id to check "did my recent syncs
actually run".

    python check_recent_ingestion_runs.py
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import async_session_factory, set_tenant_context
from app.ingestion import repository

# Any connector_config_id belonging to the org -- only used to resolve which
# organization to scope this read to.
_KNOWN_CONNECTOR_CONFIG_ID = uuid.UUID("9b21a991-1de5-4253-9fbf-a80c992f3026")
_LIMIT = 15


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
                    j.id AS job_id,
                    j.connector_config_id,
                    c.source,
                    c.config->'repos'->0->>'repo' AS repo,
                    j.status,
                    j.failed_stage,
                    j.last_error_type,
                    j.started_at,
                    j.completed_at,
                    j.documents_processed,
                    j.chunks_embedded
                FROM ingestion_jobs j
                LEFT JOIN connector_configs c ON c.id = j.connector_config_id
                WHERE j.organization_id = :org_id
                ORDER BY j.started_at DESC NULLS LAST
                LIMIT :limit
                """
            ),
            {"org_id": str(organization_id), "limit": _LIMIT},
        )
        rows = result.mappings().all()

        if not rows:
            print("No ingestion_jobs rows found for this organization at all.")
            return

        print(f"Most recent {len(rows)} ingestion run(s), newest first:\n")
        for row in rows:
            print("---")
            for key, value in row.items():
                print(f"  {key}: {value}")


if __name__ == "__main__":
    asyncio.run(main())
