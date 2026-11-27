"""retention support: batched MCP purge and document tombstones

Revision ID: d9b2f4a7c1e3
Revises: c1a5e7f3b9d2
Create Date: 2026-11-29 10:00:00.000000

Additive. Supports the scheduled retention / document lifecycle work.

* `purge_mcp_requests_batch(cutoff, batch_size)` -- `SECURITY DEFINER` (pinned `search_path`, EXECUTE revoked
  from PUBLIC and granted to `ekip_app`), the BATCHED counterpart of the existing `purge_mcp_requests(cutoff)`
  (which deletes everything in one statement). Rows with no organization (calls that failed before an identity
  existed) can only be reached through a definer function, so the scheduled job uses this one in a loop.
  The old function is left in place.
* `documents.tombstoned_at` (nullable) -- set when an old, superseded version's metadata was removed by the
  version-retention policy. The `documents` row itself is never deleted, so every citation keeps resolving.

Downgrade drops the function and the column; no document is deleted.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'd9b2f4a7c1e3'
down_revision: str | None = 'c1a5e7f3b9d2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION public.purge_mcp_requests_batch(cutoff timestamptz, batch_size integer)
        RETURNS bigint
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, public, pg_temp
        AS $$
            WITH doomed AS (
                SELECT ctid FROM public.mcp_requests
                WHERE occurred_at < cutoff
                LIMIT GREATEST(batch_size, 1)
            ), deleted AS (
                DELETE FROM public.mcp_requests m USING doomed d WHERE m.ctid = d.ctid RETURNING 1
            )
            SELECT count(*) FROM deleted;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.purge_mcp_requests_batch(timestamptz, integer) FROM PUBLIC")
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ekip_app') THEN
                GRANT EXECUTE ON FUNCTION public.purge_mcp_requests_batch(timestamptz, integer) TO ekip_app;
            END IF;
        END $$
        """
    )

    op.add_column('documents', sa.Column('tombstoned_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('documents', 'tombstoned_at')
    op.execute("DROP FUNCTION IF EXISTS public.purge_mcp_requests_batch(timestamptz, integer)")
