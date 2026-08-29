"""agent_memories: persistent, permission-aware agent memory (Priority 4)

Revision ID: f1a2b3c4d5e6
Revises: c5e2a9f4d7b3
Create Date: 2026-08-25 00:00:00.000000

Adds the one table backing `app.core.memory` -- see
`app/database/models/memory_models.py` for the full column-by-column
rationale and `docs/AGENT_MEMORY.md` for the architecture.

Two decisions in this migration are worth stating here rather than only in
the model:

1. THE EMBEDDING IS A COLUMN ON THIS TABLE, not a child table. Priority 3
   found a real bug (see `docs/DATA_LIFECYCLE.md` section 5) where a
   soft-deleted `documents` row left derived chunk/embedding rows behind in
   separate tables, and those stayed semantically retrievable. Co-locating
   the vector with the record makes that impossible here: there is no
   separate row that can be orphaned. `VECTOR(384)` matches
   `retrieval_models._EMBEDDING_DIMENSION` and
   `retrieval.embedding.EMBEDDING_DIMENSION`; changing the embedding model
   means migrating this column in lockstep with the three `*_chunks` tables
   (ENGINEERING_DECISIONS.md #006).

   Requires the `vector` extension, already enabled by the initial migration
   (`be0234931e65`) -- not re-created here.

2. RLS IS APPLIED IN THIS MIGRATION, not left for later. `agent_memories`
   carries its own `organization_id`, so it belongs in exactly the same
   `tenant_isolation` policy set migration `c7d4e8f19a2b` created for every
   other direct-`organization_id` table. Adding a new tenant-scoped table
   without its policy would silently create the first table in the schema
   that RLS does not cover -- a gap that would be invisible until someone
   went looking. The policy text is duplicated from `c7d4e8f19a2b` verbatim
   (same GUC, same policy name, same clause shape) rather than imported:
   Alembic revisions must stay independently replayable, so a later edit to
   that migration can never retroactively change what this one did.

   Note this table needs RLS *more* than most: it is the first table in the
   schema holding rows that are private to an individual user within an
   organization, not merely scoped to the organization.

No index on `embedding`. Per-organization memory volume is small by design
(memory is explicitly curated, never harvested from every message), so an
exact nearest-neighbour scan over an already-tenant-filtered handful of rows
is both faster than an ANN probe and exactly accurate. An HNSW/IVFFlat index
would additionally make recall *approximate*, which is a bad trade for a
permission-sensitive result set. Revisit only with real volume evidence.
"""
from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: str | None = 'c5e2a9f4d7b3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMBEDDING_DIMENSION = 384

# Duplicated from `c7d4e8f19a2b` on purpose -- see this module's docstring.
_GUC_NAME = 'app.current_organization_id'
_POLICY_NAME = 'tenant_isolation'
_TABLE = 'agent_memories'


def upgrade() -> None:
    # Table name written as a literal, not via `_TABLE`, on purpose:
    # `tests/database/test_migration_coverage.py` statically scans these
    # files for `op.create_table('name')` to catch a model that no migration
    # creates (a real production outage once shipped that way -- see that
    # test's docstring). A variable here would be invisible to that scan and
    # would silently disarm the guard for this table.
    op.create_table(
        'agent_memories',
        sa.Column(
            'id',
            sa.UUID(),
            server_default=sa.text('gen_random_uuid()'),
            nullable=False,
        ),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('scope', sa.Text(), nullable=False),
        sa.Column('owner_user_id', sa.UUID(), nullable=True),
        sa.Column('project_id', sa.UUID(), nullable=True),
        sa.Column('memory_type', sa.Text(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column(
            'embedding',
            pgvector.sqlalchemy.Vector(_EMBEDDING_DIMENSION),
            nullable=False,
        ),
        sa.Column('source_type', sa.Text(), nullable=True),
        sa.Column('source_id', sa.UUID(), nullable=True),
        sa.Column('created_by', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('supersedes_memory_id', sa.UUID(), nullable=True),
        sa.Column('metadata', sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column('last_accessed_at', sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(
            ['supersedes_memory_id'], ['agent_memories.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_agent_memories_org_status', _TABLE, ['organization_id', 'status'], unique=False
    )
    op.create_index('ix_agent_memories_owner_user_id', _TABLE, ['owner_user_id'], unique=False)
    op.create_index(
        'ix_agent_memories_org_project', _TABLE, ['organization_id', 'project_id'], unique=False
    )

    # Same tenant-isolation policy every other direct-organization_id table
    # already carries (`c7d4e8f19a2b`). FORCE so the policy also applies to
    # the table owner, not just to other roles.
    op.execute(f'ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY {_POLICY_NAME} ON {_TABLE} '
        f"USING (organization_id = current_setting('{_GUC_NAME}', true)::uuid)"
    )


def downgrade() -> None:
    op.execute(f'DROP POLICY IF EXISTS {_POLICY_NAME} ON {_TABLE}')
    op.execute(f'ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY')
    op.drop_index('ix_agent_memories_org_project', table_name=_TABLE)
    op.drop_index('ix_agent_memories_owner_user_id', table_name=_TABLE)
    op.drop_index('ix_agent_memories_org_status', table_name=_TABLE)
    op.drop_table(_TABLE)
