"""storage monitoring snapshots and the audit-archive ledger

Revision ID: e1c7a9d3b5f2
Revises: d9b2f4a7c1e3
Create Date: 2026-11-29 12:00:00.000000

Additive: four new tables and one read function. **No existing table is altered, and `audit_logs` is not
touched at all.**

Storage monitoring
* `storage_snapshots` -- one row per daily snapshot: the MEASURED size of the whole database
  (`pg_database_size`), the configured storage limit (nullable: it is never guessed), the percentage and whether
  the warning threshold was reached.
* `storage_snapshot_tables` -- MEASURED per-table sizes (`pg_total_relation_size`, table, indexes) plus the
  planner's row ESTIMATE (`reltuples`) and a category (`chunks`, `documents`, `audit`, `other`).
* `storage_snapshot_tenants` -- per-organization ESTIMATES (row counts are exact, bytes are a proportional
  estimate; `basis` says so). Tenant-owned: `FORCE ROW LEVEL SECURITY`, so an organization reads only its own.
* `platform_storage_tenants(operator, limit)` -- `SECURITY DEFINER` (pinned `search_path`, EXECUTE only for
  `ekip_app`) read of the latest snapshot's largest tenants for platform operators; verifies the operator
  inside the database like the other platform read functions.

Audit archive ledger
* `audit_archive_batches` -- one row per (organization, month) exported by `scripts/archive_audit_logs.py`:
  row count, byte size, SHA-256, location, status (`exported` -> `verified` | `failed`). It records that an
  export exists and was verified; **nothing ever deletes or rewrites an audit row.** Tenant-owned, FORCE RLS.

Downgrade drops the function and the four tables.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'e1c7a9d3b5f2'
down_revision: str | None = 'd9b2f4a7c1e3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_TABLES = ('storage_snapshot_tenants', 'audit_archive_batches')

_FN = """
CREATE FUNCTION public.platform_storage_tenants(p_operator_user_id uuid, p_limit integer DEFAULT 20)
RETURNS TABLE (
    out_organization_id uuid, out_name text, out_slug text, out_estimated_bytes bigint,
    out_chunk_rows bigint, out_document_rows bigint, out_basis text, out_taken_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_snapshot uuid;
BEGIN
    IF p_operator_user_id IS NULL OR public.platform_operator_role(p_operator_user_id) IS NULL THEN
        RAISE EXCEPTION 'caller is not an active platform operator' USING ERRCODE = 'EK003';
    END IF;
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 200 THEN
        RAISE EXCEPTION 'invalid limit' USING ERRCODE = 'EK005';
    END IF;
    SELECT s.id INTO v_snapshot FROM public.storage_snapshots s ORDER BY s.taken_at DESC LIMIT 1;
    RETURN QUERY
    SELECT t.organization_id, o.name, o.slug, t.estimated_bytes, t.chunk_rows, t.document_rows, t.basis, s.taken_at
      FROM public.storage_snapshot_tenants t
      JOIN public.storage_snapshots s ON s.id = t.snapshot_id
      JOIN public.organizations o ON o.id = t.organization_id
     WHERE t.snapshot_id = v_snapshot
     ORDER BY t.estimated_bytes DESC NULLS LAST, t.organization_id
     LIMIT p_limit;
END
$$;
"""


def upgrade() -> None:
    op.create_table(
        'storage_snapshots',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('taken_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('database_bytes', sa.BigInteger(), nullable=False),
        sa.Column('storage_limit_bytes', sa.BigInteger(), nullable=True),
        sa.Column('percent_of_limit', sa.Numeric(6, 2), nullable=True),
        sa.Column('warning', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('warning_percent', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_storage_snapshots_taken_at', 'storage_snapshots', ['taken_at'])

    op.create_table(
        'storage_snapshot_tables',
        sa.Column('snapshot_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('table_name', sa.Text(), nullable=False),
        sa.Column('category', sa.Text(), nullable=False),
        sa.Column('total_bytes', sa.BigInteger(), nullable=False),
        sa.Column('table_bytes', sa.BigInteger(), nullable=False),
        sa.Column('index_bytes', sa.BigInteger(), nullable=False),
        sa.Column('row_estimate', sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(['snapshot_id'], ['storage_snapshots.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('snapshot_id', 'table_name'),
    )

    op.create_table(
        'storage_snapshot_tenants',
        sa.Column('snapshot_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('organization_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('chunk_rows', sa.BigInteger(), nullable=False),
        sa.Column('document_rows', sa.BigInteger(), nullable=False),
        sa.Column('estimated_bytes', sa.BigInteger(), nullable=True),
        sa.Column('basis', sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(['snapshot_id'], ['storage_snapshots.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('snapshot_id', 'organization_id'),
    )
    op.create_index('ix_storage_snapshot_tenants_org', 'storage_snapshot_tenants', ['organization_id', 'snapshot_id'])

    op.create_table(
        'audit_archive_batches',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('period_start', sa.Date(), nullable=False),
        sa.Column('period_end', sa.Date(), nullable=False),
        sa.Column('row_count', sa.BigInteger(), nullable=False),
        sa.Column('byte_size', sa.BigInteger(), nullable=True),
        sa.Column('sha256', sa.Text(), nullable=True),
        sa.Column('location', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('exported_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("status IN ('exported', 'verified', 'failed')", name='ck_audit_archive_status'),
        sa.UniqueConstraint('organization_id', 'period_start', name='uq_audit_archive_org_period'),
    )

    for table in _TENANT_TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON public.{table} "
            "USING (organization_id = current_setting('app.current_organization_id', true)::uuid)"
        )

    op.execute(_FN)
    op.execute("REVOKE ALL ON FUNCTION public.platform_storage_tenants(uuid, integer) FROM PUBLIC")
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ekip_app') THEN
                GRANT EXECUTE ON FUNCTION public.platform_storage_tenants(uuid, integer) TO ekip_app;
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.platform_storage_tenants(uuid, integer)")
    op.drop_table('audit_archive_batches')
    op.drop_index('ix_storage_snapshot_tenants_org', table_name='storage_snapshot_tenants')
    op.drop_table('storage_snapshot_tenants')
    op.drop_table('storage_snapshot_tables')
    op.drop_index('ix_storage_snapshots_taken_at', table_name='storage_snapshots')
    op.drop_table('storage_snapshots')
