"""incidents_chunks: the missing "incidents" retrieval collection

Revision ID: d4e5f6a7b8c9
Revises: b1c2d3e4f5a6
Create Date: 2026-09-05 00:00:00.000000

Closes EKIP audit finding 6: `retrieval.schemas.CollectionName` had no
`"incidents"` entry and nothing produced embeddable chunks for one (see
`app.database.models.retrieval_models`'s module docstring, previously
documenting this exact gap). `app.ingestion.connectors.incidents.
IncidentsConnector` re-ingests each organization's incidents (title +
description + resolution, when an approved/published postmortem exists)
through the ordinary ingestion pipeline into this table -- the same shape
every other `<collection>_chunks` table already has (`_ChunkColumns` in
`retrieval_models.py`), so it needs no special-cased query logic in
`PgVectorStore`.

RLS: same `tenant_isolation` policy, same direct `organization_id` column
compare, as every other `_DIRECT_TABLES` entry in `c7d4e8f19a2b` (this
table has its own `organization_id` column, denormalized at upsert time
like every other chunk table -- no join-scoped policy needed). Enabled in
this same migration, not a follow-up one, so the table is never RLS-less
even transiently between migrations.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import pgvector

# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: str | None = 'b1c2d3e4f5a6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUC_NAME = 'app.current_organization_id'
_POLICY_NAME = 'tenant_isolation'


def upgrade() -> None:
    op.create_table('incidents_chunks',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('organization_id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('chunk_index', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=False),
    sa.Column('source_offset_start', sa.Integer(), nullable=False),
    sa.Column('source_offset_end', sa.Integer(), nullable=False),
    sa.Column('acl_permission_code', sa.Text(), nullable=True),
    sa.Column('content_tsv', postgresql.TSVECTOR(), sa.Computed("to_tsvector('english', content)", persisted=True), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('document_id', 'chunk_index', name='uq_incidents_chunks_document_chunk_index')
    )
    op.create_index('ix_incidents_chunks_content_tsv', 'incidents_chunks', ['content_tsv'], unique=False, postgresql_using='gin')
    op.create_index('ix_incidents_chunks_org_project', 'incidents_chunks', ['organization_id', 'project_id'], unique=False)

    op.execute('ALTER TABLE incidents_chunks ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE incidents_chunks FORCE ROW LEVEL SECURITY')
    op.execute(
        f"CREATE POLICY {_POLICY_NAME} ON incidents_chunks "
        f"USING (organization_id = current_setting('{_GUC_NAME}', true)::uuid)"
    )


def downgrade() -> None:
    op.execute(f'DROP POLICY IF EXISTS {_POLICY_NAME} ON incidents_chunks')
    op.execute('ALTER TABLE incidents_chunks NO FORCE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE incidents_chunks DISABLE ROW LEVEL SECURITY')
    op.drop_index('ix_incidents_chunks_org_project', table_name='incidents_chunks')
    op.drop_index('ix_incidents_chunks_content_tsv', table_name='incidents_chunks')
    op.drop_table('incidents_chunks')
