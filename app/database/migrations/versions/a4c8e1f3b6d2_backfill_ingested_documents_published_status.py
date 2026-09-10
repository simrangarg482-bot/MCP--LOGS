"""backfill_ingested_documents_published_status

Revision ID: a4c8e1f3b6d2
Revises: f3e7c05b146e
Create Date: 2026-09-10 12:42:58.648036

Data-only migration -- no schema change. `documents.status` has no database
CHECK constraint (the vocabulary lives in `app.shared.schemas.common.
DocumentStatus` at the application layer only -- see `core/knowledge/
repository.py`'s own module docstring for why), so this migration is a
plain `UPDATE`, nothing more.

Context (Stage 1 of the Knowledge/Knowledge Gaps review): `app.ingestion.
repository.insert_document` has always inserted every connector-synced row
(GitHub/Slack/Jira/etc. -- every `source` value except `"manual"`) at
`status="proposed"`, and no code path anywhere ever transitioned one of
those rows to `"published"`: only `core.knowledge.service.publish_document`
(the human-review `/knowledge/review` action) does that, and it is
reachable only for `source="manual"` documents created via `core.knowledge.
repository.insert_document`/`propose_runbook_update`, a disjoint writer of
the same table.

Retrieval was never gated on this status -- `app.ingestion.service` embeds
chunks into the `documentation`/`code` collections unconditionally at sync
time, regardless of `status` -- so this was never a search-correctness bug.
But it left every ingested document permanently stuck in the
`/knowledge/review` queue (unbounded, growing with every sync, and not
meaningfully reviewable there in practice: connector-sourced rows never
populate the `document_metadata` `"content"` key the review UI renders) and
permanently invisible from `/knowledge`'s "browse published knowledge" page,
which exists specifically to show this content.

This migration is the one-time backfill for every row already affected by
that gap. The accompanying code fix (`app.ingestion.repository.
insert_document` now defaults new rows to `status="published"`) stops new
rows from accumulating the same way going forward -- this migration only
repairs rows written before that fix.

Scoped to `source <> 'manual'` so it never touches a document that came
through the real human/agent-proposed review path -- those keep whatever
status the actual review flow already gave them. `deleted_at IS NULL`
excludes already-rejected (soft-deleted) documents, which should stay
exactly as rejection left them.

Idempotent: the `WHERE status = 'proposed'` clause means a second run
matches zero rows and is a safe no-op.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a4c8e1f3b6d2'
down_revision: str | None = 'f3e7c05b146e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE documents
        SET status = 'published'
        WHERE status = 'proposed'
          AND source <> 'manual'
          AND deleted_at IS NULL
        """
    )


def downgrade() -> None:
    """Best-effort reversal only. Reverts every `source <> 'manual'` row
    currently `status='published'` back to `'proposed'` -- indistinguishable,
    after the fact, from a connector-sourced document that was legitimately
    published through a real human review before this migration ever ran
    (if any such row exists, this downgrade incorrectly reverts it too).
    Accepted: there is no stored marker that would make this exact, and
    downgrading this specific data backfill is not expected to be a real
    operational path.
    """
    op.execute(
        """
        UPDATE documents
        SET status = 'proposed'
        WHERE status = 'published'
          AND source <> 'manual'
        """
    )
