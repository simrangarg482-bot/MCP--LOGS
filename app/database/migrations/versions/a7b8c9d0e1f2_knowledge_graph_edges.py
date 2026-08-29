"""knowledge_graph_edges: derived relationship layer (Priority 5)

Revision ID: a7b8c9d0e1f2
Revises: f1a2b3c4d5e6
Create Date: 2026-08-25 00:00:00.000000

One table backing `app.core.graph`. See
`app/database/models/graph_models.py` for the column-by-column rationale and
`docs/KNOWLEDGE_GRAPH.md` for the architecture.

Three decisions worth recording here rather than only in the model:

1. ONLY NON-FOREIGN-KEY RELATIONSHIPS LIVE IN THIS TABLE. Relationships
   Postgres already enforces (`postmortems.incident_id`,
   `incidents.project_id`, `documents.project_id`,
   `incident_timeline.incident_id`) are resolved live at traversal time and
   never stored. Storing copies of an FK could only add staleness and a leak
   path -- the Priority 3 failure mode where derived rows outlived their
   source. See `app.core.graph.contract`.

2. THE UNIQUE CONSTRAINT EXCLUDES PROVENANCE AND STATUS. The logical
   identity of a relationship is
   `(organization, source, relationship_type, target)`. If the same
   relationship is re-discovered -- by a repeat discovery pass, or by a
   different mechanism -- that is the same fact, so the upsert must converge
   on the existing row rather than accumulate near-duplicates. Excluding
   `status` too means a re-discovered edge revives its own soft-deleted row
   instead of colliding with it.

3. NO FOREIGN KEYS ON THE ENDPOINT COLUMNS. `source_entity_id`/
   `target_entity_id` are plain UUIDs because one column cannot reference
   four tables -- the same polymorphic tradeoff `audit_logs.resource_id`
   already makes in this schema. The consequence is handled explicitly
   rather than ignored: traversal resolves both endpoints against their live
   source tables and drops anything that no longer resolves, so a stale edge
   is inert even before cleanup removes it.

RLS is applied here, in this migration, for the same reason Priority 4's
`agent_memories` migration did: adding a tenant-scoped table without its
policy would silently create the first table in the schema RLS does not
cover. Policy text is duplicated from `c7d4e8f19a2b` verbatim rather than
imported -- Alembic revisions must stay independently replayable.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: str | None = 'f1a2b3c4d5e6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Duplicated from `c7d4e8f19a2b` on purpose -- see this module's docstring.
_GUC_NAME = 'app.current_organization_id'
_POLICY_NAME = 'tenant_isolation'


def upgrade() -> None:
    # Table name as a literal, not a variable:
    # `tests/database/test_migration_coverage.py` statically scans for
    # `op.create_table('name')` to catch a model with no creating migration
    # (a real production outage once shipped that way). A variable would be
    # invisible to that scan and would disarm the guard for this table.
    op.create_table(
        'knowledge_graph_edges',
        sa.Column(
            'id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False
        ),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('project_id', sa.UUID(), nullable=True),
        sa.Column('source_entity_type', sa.Text(), nullable=False),
        sa.Column('source_entity_id', sa.UUID(), nullable=False),
        sa.Column('relationship_type', sa.Text(), nullable=False),
        sa.Column('target_entity_type', sa.Text(), nullable=False),
        sa.Column('target_entity_id', sa.UUID(), nullable=False),
        sa.Column('provenance_type', sa.Text(), nullable=False),
        sa.Column('provenance_id', sa.UUID(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('created_by', sa.Text(), nullable=False),
        sa.Column('metadata', sa.dialects.postgresql.JSONB(), nullable=True),
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
            'organization_id',
            'source_entity_type',
            'source_entity_id',
            'relationship_type',
            'target_entity_type',
            'target_entity_id',
            name='uq_knowledge_graph_edges_logical_identity',
        ),
    )
    op.create_index(
        'ix_kg_edges_org_source',
        'knowledge_graph_edges',
        ['organization_id', 'source_entity_type', 'source_entity_id', 'status'],
        unique=False,
    )
    op.create_index(
        'ix_kg_edges_org_target',
        'knowledge_graph_edges',
        ['organization_id', 'target_entity_type', 'target_entity_id', 'status'],
        unique=False,
    )
    op.create_index(
        'ix_kg_edges_org_status',
        'knowledge_graph_edges',
        ['organization_id', 'status'],
        unique=False,
    )

    op.execute('ALTER TABLE knowledge_graph_edges ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE knowledge_graph_edges FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY {_POLICY_NAME} ON knowledge_graph_edges '
        f"USING (organization_id = current_setting('{_GUC_NAME}', true)::uuid)"
    )


def downgrade() -> None:
    op.execute(f'DROP POLICY IF EXISTS {_POLICY_NAME} ON knowledge_graph_edges')
    op.execute('ALTER TABLE knowledge_graph_edges NO FORCE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE knowledge_graph_edges DISABLE ROW LEVEL SECURITY')
    op.drop_index('ix_kg_edges_org_status', table_name='knowledge_graph_edges')
    op.drop_index('ix_kg_edges_org_target', table_name='knowledge_graph_edges')
    op.drop_index('ix_kg_edges_org_source', table_name='knowledge_graph_edges')
    op.drop_table('knowledge_graph_edges')
