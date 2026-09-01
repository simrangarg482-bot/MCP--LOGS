"""Soft-delete already-ingested documents that today's connector filter
would skip -- vendored dependency trees and generated build output
(`site-packages/`, `.venv*/`, `node_modules/`, `dist/`, ...).

`app.ingestion.connectors.github.GitHubConnector._is_skipped` stops these at
fetch time now, but repositories synced before that filter existed left
thousands of third-party files in the knowledge base, where they pollute
retrieval and bury the human review queue. Retrieval and the review queue
both filter `documents.deleted_at IS NULL`, so a soft delete removes them
from both immediately and is reversible.

Usage (dry run by default)::

    python scripts/prune_vendored_documents.py                 # every org, report only
    python scripts/prune_vendored_documents.py --apply         # every org, soft-delete
    python scripts/prune_vendored_documents.py --org <uuid> --apply
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update

from app.database.models.ingestion_models import Document, DocumentMetadata
from app.database.session import session_scope
from app.ingestion.connectors.github import GitHubConnector


async def _collect_prunable(
    session, organization_id: uuid.UUID | None
) -> list[tuple[uuid.UUID, str]]:
    """Return `(document_id, path)` for every live GitHub document whose
    stored `path` metadata the connector would skip today.
    """
    stmt = (
        select(Document.id, DocumentMetadata.value)
        .join(DocumentMetadata, DocumentMetadata.document_id == Document.id)
        .where(
            Document.source == "github",
            Document.deleted_at.is_(None),
            DocumentMetadata.key == "path",
        )
    )
    if organization_id is not None:
        stmt = stmt.where(Document.organization_id == organization_id)

    rows = (await session.execute(stmt)).all()
    return [(doc_id, path) for doc_id, path in rows if GitHubConnector._is_skipped(path)]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", type=uuid.UUID, default=None, help="limit to one organization id")
    parser.add_argument(
        "--apply", action="store_true", help="perform the soft delete (default: dry run)"
    )
    args = parser.parse_args()

    async with session_scope() as session:
        prunable = await _collect_prunable(session, args.org)
        print(f"{len(prunable)} document(s) match the connector's skip filter")
        for _, path in prunable[:15]:
            print(f"  - {path}")
        if len(prunable) > 15:
            print(f"  ... and {len(prunable) - 15} more")

        if not prunable:
            return
        if not args.apply:
            print("\ndry run -- re-run with --apply to soft-delete these")
            return

        document_ids = [doc_id for doc_id, _ in prunable]
        now = datetime.now(UTC)
        for start in range(0, len(document_ids), 500):
            batch = document_ids[start : start + 500]
            await session.execute(
                update(Document)
                .where(Document.id.in_(batch), Document.deleted_at.is_(None))
                .values(deleted_at=now)
            )
        await session.commit()
        print(f"\nsoft-deleted {len(document_ids)} document(s) (deleted_at={now.isoformat()})")


if __name__ == "__main__":
    asyncio.run(main())
