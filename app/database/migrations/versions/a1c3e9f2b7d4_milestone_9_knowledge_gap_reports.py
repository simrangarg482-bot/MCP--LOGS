"""milestone 9: knowledge_gap_reports

Revision ID: a1c3e9f2b7d4
Revises: f8698cb5abae
Create Date: 2026-08-05 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a1c3e9f2b7d4'
down_revision: str | None = 'f8698cb5abae'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'knowledge_gap_reports',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('suggested_topic', sa.Text(), nullable=False),
        sa.Column('topic_embedding', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            'supporting_execution_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column('suggested_action', sa.Text(), nullable=False),
        sa.Column('related_document_id', sa.UUID(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False, server_default='open'),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['related_document_id'], ['documents.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_knowledge_gap_reports_org_status',
        'knowledge_gap_reports',
        ['organization_id', 'status'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_knowledge_gap_reports_org_status', table_name='knowledge_gap_reports')
    op.drop_table('knowledge_gap_reports')
