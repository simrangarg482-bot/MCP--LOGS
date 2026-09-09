"""One-off, read-only diagnostic: for the org that owns a known connector
id, list every connector_configs row with its target repo (from its config
JSON), the connector's own status/last_synced_at, its most recent
ingestion_jobs rows (status, items_discovered vs. items_skipped vs.
documents_processed, last_error_type/failed_stage), and how many documents
currently exist for that repo -- so we can see whether a connector that
looks "stuck" at a low doc count actually discovered more items than it
ingested (a real bug) or simply never discovered more (nothing to ingest,
or never re-run).

    python check_connector_repo_map.py
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import async_session_factory, set_tenant_context
from app.ingestion import repository

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
                SELECT id, status, config, last_synced_at, created_at, updated_at
                FROM connector_configs
                WHERE organization_id = :org_id
                ORDER BY created_at
                """
            ),
            {"org_id": str(organization_id)},
        )
        rows = result.mappings().all()

        print(f"Organization: {organization_id}\n")
        for row in rows:
            config = row["config"] or {}
            repo = config.get("repo") or config.get("repository") or config.get("full_name")
            print(f"connector_config_id: {row['id']}")
            print(f"  status: {row['status']}")
            print(f"  last_synced_at: {row['last_synced_at']}")
            print(f"  config: {config}")

            doc_result = await session.execute(
                text(
                    """
                    SELECT count(*) AS n, max(created_at) AS latest
                    FROM documents
                    WHERE organization_id = :org_id
                      AND source = 'github'
                      AND source_url LIKE '%' || :repo_frag || '%'
                    """
                ),
                {"org_id": str(organization_id), "repo_frag": repo or "___none___"},
            )
            doc_row = doc_result.mappings().one()
            print(f"  documents matching this repo fragment: {doc_row['n']} (latest: {doc_row['latest']})")

            job_result = await session.execute(
                text(
                    """
                    SELECT status, started_at, completed_at, failed_stage,
                           last_error_type, items_discovered, items_skipped,
                           documents_processed, pages_fetched, retry_count
                    FROM ingestion_jobs
                    WHERE connector_config_id = :cid
                    ORDER BY created_at DESC
                    LIMIT 5
                    """
                ),
                {"cid": str(row["id"])},
            )
            print("  recent ingestion_jobs:")
            for j in job_result.mappings().all():
                print(
                    f"    status={j['status']} started={j['started_at']} completed={j['completed_at']} "
                    f"discovered={j['items_discovered']} skipped={j['items_skipped']} "
                    f"processed={j['documents_processed']} pages={j['pages_fetched']} "
                    f"retries={j['retry_count']} failed_stage={j['failed_stage']} "
                    f"last_error_type={j['last_error_type']}"
                )
            print()


if __name__ == "__main__":
    asyncio.run(main())
