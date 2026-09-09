"""One-off, read-only diagnostic: print the FULL current config/checkpoint
JSON for the mcp-logs connector, plus every ingestion_jobs row ever
recorded for it (not just the last 5) -- to see whether a real full sync
ever actually finished for this connector, or whether the current
`discovered=0` reconciliation runs are trusting a checkpoint left behind by
a run that never completed.

    python check_mcp_logs_checkpoint.py
"""

from __future__ import annotations

import asyncio
import json
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
        await set_tenant_context(session, organization_id)

        row = (
            await session.execute(
                text("SELECT config FROM connector_configs WHERE id = :cid"),
                {"cid": str(_CONNECTOR_CONFIG_ID)},
            )
        ).mappings().one()
        print("Full config JSON:")
        print(json.dumps(row["config"], indent=2, default=str))

        jobs = (
            await session.execute(
                text(
                    """
                    SELECT status, created_at, started_at, completed_at,
                           items_discovered, items_skipped, documents_processed,
                           pages_fetched, retry_count, failed_stage, last_error_type
                    FROM ingestion_jobs
                    WHERE connector_config_id = :cid
                    ORDER BY created_at ASC
                    """
                ),
                {"cid": str(_CONNECTOR_CONFIG_ID)},
            )
        ).mappings().all()

        print(f"\nAll {len(jobs)} ingestion_jobs rows for this connector, oldest first:")
        for j in jobs:
            print(
                f"  {j['created_at']} status={j['status']} discovered={j['items_discovered']} "
                f"skipped={j['items_skipped']} processed={j['documents_processed']} "
                f"pages={j['pages_fetched']} retries={j['retry_count']} "
                f"failed_stage={j['failed_stage']} last_error_type={j['last_error_type']}"
            )


if __name__ == "__main__":
    asyncio.run(main())
