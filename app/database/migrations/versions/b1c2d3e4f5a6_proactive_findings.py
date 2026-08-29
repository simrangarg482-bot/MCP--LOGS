"""proactive_findings / proactive_finding_evidence: derived pattern-detection
layer (Priority 6)

Revision ID: b1c2d3e4f5a6
Revises: a7b8c9d0e1f2
Create Date: 2026-08-25 00:00:00.000000

See `app/database/models/pattern_models.py` for the column-by-column
rationale and `docs/PROACTIVE_INTELLIGENCE.md` for the architecture.

Two decisions worth recording here rather than only in the model:

1. `proactive_finding_evidence` gets a JOIN-based RLS policy, not a direct
   `organization_id` column compare -- the identical shape `document_
   metadata`/`project_memberships` already use in `c7d4e8f19a2b`, since
   this table has no `organization_id` column of its own (meaningless
   without its parent finding).

2. `ON DELETE CASCADE` from evidence to its finding (unlike
   `knowledge_graph_edges`, which has no child table at all): an evidence
   row has no independent meaning once its finding is gone, the same
   reasoning `incident_timeline` already applies to its parent `incidents`
   row.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: str | None = 'a7b8c9d0e1f2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Duplicated from `c7d4e8f19a2b` on purpose -- Alembic revisions must stay
# independently replayable, the same reasoning `a7b8c9d0e1f2` gives for its
# own duplicated policy text.
_GUC_NAME = 'app.current_organization_id'
_POLICY_NAME = 'tenant_isolation'


def upgrade() -> None:
    # Literal table names, not variables: `tests/database/
    # test_migration_coverage.py` statically scans for
    # `op.create_table('name')` to catch a model with no creating migration.
    op.create_table(
        'proactive_findings',
        sa.Column(
            'id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False
        ),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('project_id', sa.UUID(), nullable=True),
        sa.Column('finding_type', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('fingerprint', sa.Text(), nullable=False),
        sa.Column('support_count', sa.Integer(), nullable=False),
        sa.Column('detector_name', sa.Text(), nullable=False),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('deactivated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['organization_id'], ['organizations.id'], ondelete='RESTRICT'
        ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'organization_id', 'fingerprint', name='uq_proactive_findings_fingerprint'
        ),
    )
    op.create_index(
        'ix_proactive_findings_org_status',
        'proactive_findings',
        ['organization_id', 'status'],
        unique=False,
    )
    op.create_index(
        'ix_proactive_findings_org_type',
        'proactive_findings',
        ['organization_id', 'finding_type'],
        unique=False,
    )

    op.create_table(
        'proactive_finding_evidence',
        sa.Column(
            'id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False
        ),
        sa.Column('finding_id', sa.UUID(), nullable=False),
        sa.Column('entity_type', sa.Text(), nullable=False),
        sa.Column('entity_id', sa.UUID(), nullable=False),
        sa.Column('role', sa.Text(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['finding_id'], ['proactive_findings.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'finding_id', 'entity_type', 'entity_id', 'role',
            name='uq_proactive_finding_evidence_identity',
        ),
    )
    op.create_index(
        'ix_proactive_finding_evidence_finding_id',
        'proactive_finding_evidence',
        ['finding_id'],
        unique=False,
    )
    op.create_index(
        'ix_proactive_finding_evidence_entity',
        'proactive_finding_evidence',
        ['entity_type', 'entity_id'],
        unique=False,
    )

    op.execute('ALTER TABLE proactive_findings ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE proactive_findings FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY {_POLICY_NAME} ON proactive_findings '
        f"USING (organization_id = current_setting('{_GUC_NAME}', true)::uuid)"
    )

    op.execute('ALTER TABLE proactive_finding_evidence ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE proactive_finding_evidence FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY {_POLICY_NAME} ON proactive_finding_evidence '
        "USING (finding_id IN (SELECT id FROM proactive_findings "
        f"WHERE organization_id = current_setting('{_GUC_NAME}', true)::uuid))"
    )


def downgrade() -> None:
    op.execute(f'DROP POLICY IF EXISTS {_POLICY_NAME} ON proactive_finding_evidence')
    op.execute('ALTER TABLE proactive_finding_evidence NO FORCE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE proactive_finding_evidence DISABLE ROW LEVEL SECURITY')

    op.execute(f'DROP POLICY IF EXISTS {_POLICY_NAME} ON proactive_findings')
    op.execute('ALTER TABLE proactive_findings NO FORCE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE proactive_findings DISABLE ROW LEVEL SECURITY')

    op.drop_index('ix_proactive_finding_evidence_entity', table_name='proactive_finding_evidence')
    op.drop_index(
        'ix_proactive_finding_evidence_finding_id', table_name='proactive_finding_evidence'
    )
    op.drop_table('proactive_finding_evidence')

    op.drop_index('ix_proactive_findings_org_type', table_name='proactive_findings')
    op.drop_index('ix_proactive_findings_org_status', table_name='proactive_findings')
    op.drop_table('proactive_findings')
