"""answer_feedback: outcome feedback on AI executions

Revision ID: d3b7f1a9c5e2
Revises: e1c7a9d3b5f2
Create Date: 2026-11-28 00:00:00.000000

Three changes, all additive:

1. `agent_executions.output_summary` (nullable JSONB) -- ids/counts/enums that
   describe what a successful run produced. Never answer text. See the model
   docstring for why this exists instead of an answer snapshot.

2. `uq_agent_executions_id_organization` -- UNIQUE (id, organization_id). `id`
   is already the primary key, so this adds no new restriction on data; it is
   the target the composite foreign key below needs.

3. `answer_feedback` -- one row per (execution, user). The composite foreign
   key `(execution_id, organization_id)` makes it impossible for a feedback
   row to reference another organization's execution, whatever the service
   layer does. RLS is applied here, in the creating migration, for the same
   reason `agent_memories`/`knowledge_graph_edges` did: a tenant-scoped table
   without its policy would be the first table in the schema RLS does not
   cover. Policy text is duplicated from `c7d4e8f19a2b` rather than imported
   -- revisions must stay independently replayable.

Existing rows are untouched: the new column is nullable and the constraint is
over a column that is already unique.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd3b7f1a9c5e2'
down_revision: str | None = 'e1c7a9d3b5f2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Duplicated from `c7d4e8f19a2b` on purpose -- see this module's docstring.
_GUC_NAME = 'app.current_organization_id'
_POLICY_NAME = 'tenant_isolation'


def upgrade() -> None:
    op.add_column(
        'agent_executions',
        sa.Column('output_summary', sa.dialects.postgresql.JSONB(), nullable=True),
    )
    op.create_unique_constraint(
        'uq_agent_executions_id_organization', 'agent_executions', ['id', 'organization_id']
    )

    # Table name as a literal: `tests/database/test_migration_coverage.py`
    # statically scans for `op.create_table('name')`.
    op.create_table(
        'answer_feedback',
        sa.Column(
            'id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False
        ),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('execution_id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('rating', sa.Text(), nullable=False),
        sa.Column('reason_code', sa.Text(), nullable=True),
        sa.Column('comment', sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ['execution_id', 'organization_id'],
            ['agent_executions.id', 'agent_executions.organization_id'],
            name='fk_answer_feedback_execution_same_organization',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'execution_id', 'user_id', name='uq_answer_feedback_execution_user'
        ),
        sa.CheckConstraint(
            "rating IN ('helpful', 'not_helpful')", name='ck_answer_feedback_rating'
        ),
        sa.CheckConstraint(
            'comment IS NULL OR char_length(comment) <= 2000',
            name='ck_answer_feedback_comment_length',
        ),
    )
    op.create_index(
        'ix_answer_feedback_org_created_at',
        'answer_feedback',
        ['organization_id', 'created_at'],
        unique=False,
    )
    op.create_index(
        'ix_answer_feedback_org_rating',
        'answer_feedback',
        ['organization_id', 'rating'],
        unique=False,
    )
    op.create_index('ix_answer_feedback_user_id', 'answer_feedback', ['user_id'], unique=False)

    op.execute('ALTER TABLE answer_feedback ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE answer_feedback FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY {_POLICY_NAME} ON answer_feedback '
        f"USING (organization_id = current_setting('{_GUC_NAME}', true)::uuid)"
    )


def downgrade() -> None:
    op.execute(f'DROP POLICY IF EXISTS {_POLICY_NAME} ON answer_feedback')
    op.execute('ALTER TABLE answer_feedback NO FORCE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE answer_feedback DISABLE ROW LEVEL SECURITY')
    op.drop_table('answer_feedback')
    op.drop_constraint(
        'uq_agent_executions_id_organization', 'agent_executions', type_='unique'
    )
    op.drop_column('agent_executions', 'output_summary')
