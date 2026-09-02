"""batch_4_6_remove_orphaned_simran_ekip_branch_objects

Revision ID: 90ff736ced55
Revises: e2b3c4d5f6a7
Create Date: 2026-08-18 10:20:13.191448

DRAFT -- NOT YET APPLIED to the real Neon database as of authoring. See
docs/operations/migration-recovery.md for the full Batch 4.5/4.6
investigation this closes part of.

`upgrade()` is idempotent (2026-09-02): every object it removes is guarded
with an existence check, so it is a clean no-op on any database the
`origin/simran-ekip` branch never touched -- a freshly created CI/local
database included. Before that fix, its unconditional
`ALTER TABLE eval_runs ...` / `op.drop_column('agent_executions', ...)`
statements made `alembic upgrade head` impossible to run against a fresh
database at all (`UndefinedTable`/`UndefinedColumn`), so the migration
chain was not reproducible from scratch.

Context: the real, shared Neon development database's `alembic_version` was
found stamped to a revision (`b3d8f1a6c9e2`) that exists in no branch's
migration history. Direct schema introspection showed the database actually
contains the union of `main`'s own migration chain (ending at this
migration's parent, `e2b3c4d5f6a7`) plus four migrations that only ever
existed on the separate, never-merged `origin/simran-ekip` branch
(`f4a7c2e9b3d1` -> `b6e9c2a4f7d1` -> `d4f7b2e9c6a3` -> `d8a2f6c1b9e3`).

This migration removes ONLY the subset of that branch's objects confirmed,
by both code search and this repo's own EKIP_STRATEGIC_ANALYSIS.md, to be
forward-looking scaffolding for features `main` has not built yet and has
zero present dependency on:
  - `eval_runs` / `eval_case_results` (evaluation harness persistence,
    `d4f7b2e9c6a3`) -- `scripts/eval_confidence.py` runs today without ever
    touching either table; EKIP_STRATEGIC_ANALYSIS.md section 2.2 describes
    this persistence layer as proposed future work ("Priority: Highest. Do
    this before almost anything else"), not something already wired up.
  - `agent_executions.model_used` / `.prompt_tokens` / `.completion_tokens` /
    `.total_tokens` (per-call model routing/cost tracking, `d8a2f6c1b9e3`)
    -- EKIP_STRATEGIC_ANALYSIS.md section 2.4 describes the columns as
    already existing ahead of the routing feature itself ("the table already
    exists, just add columns"), i.e. exactly this branch's premature state.

DELIBERATELY NOT included in this migration -- do not fold these into a
future revision of this same cleanup without re-reading
docs/operations/migration-recovery.md's security section first:
  - The `ekip_app` Postgres role (`f4a7c2e9b3d1`) -- `NOSUPERUSER`/
    `NOBYPASSRLS`, i.e. exactly the non-superuser application role
    EKIP_TENANT_ISOLATION_SECURITY_REVIEW.md's recommendation #2 ("now the
    top priority") says the application must connect as for its own RLS
    policies (`c7d4e8f19a2b`) to mean anything at all. Confirmed via direct
    role introspection that `neondb_owner` -- the role `DATABASE_URL`
    actually connects as -- has `bypassrls=true`, meaning every RLS policy
    in this database is a live no-op today. Dropping the one already-correct
    role sitting in this database would erase real progress on an open,
    documented security recommendation, not clean up dead code.
  - The `incident:read` / `postmortem:read` permission catalog rows
    (`b6e9c2a4f7d1`) -- that migration's own docstring documents a confirmed
    access-control gap ("2026-08 audit 'H4'") that is CURRENTLY STILL LIVE on
    `main`: `app.core.incidents.service.get_incident` / `list_incidents` /
    `get_timeline` (and `_get_owned_incident`, which backs all three) check
    only `_ensure_same_organization` -- same-organization membership -- with
    no permission check at all, unlike every write path in the same module
    (`require_project_permission(actor, ..., _INCIDENT_WRITE_PERMISSION)`).
    Any identity in the organization, including one with zero role
    assignments, can read every incident's full detail and timeline. The
    corresponding code fix (`require_project_permission(actor, project_id,
    "incident:read")` in the read paths) exists only on `origin/simran-ekip`,
    never merged to `main`. These two catalog rows are the inert half of an
    unmerged vulnerability fix, not abandoned scaffolding -- removing them
    fixes nothing (the vulnerability is in `main`'s code, not the absence of
    these rows) and only makes a future proper fix's migration need to
    re-seed and re-backfill them to every existing role from scratch.
  - `resolve_user_first_organization(uuid)`, a `SECURITY DEFINER` function
    that exists in NO git history at all (neither branch) -- created
    directly against the live database, `GRANT EXECUTE`-ed to `ekip_app` by
    name, suggesting active, uncommitted work on the same non-superuser-
    application-role effort `ekip_app` itself represents. Left alone for the
    same reason: it may be real, further-along progress on an open security
    item, not resolved scaffolding to discard.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '90ff736ced55'
down_revision: str | None = 'e2b3c4d5f6a7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUC_NAME = 'app.current_organization_id'
_POLICY_NAME = 'tenant_isolation'
_RLS_TABLES = ['eval_runs', 'eval_case_results']


def _direct_using_clause() -> str:
    return f"organization_id = current_setting('{_GUC_NAME}', true)::uuid"


# Columns that only ever existed on the `origin/simran-ekip` branch
# (`d8a2f6c1b9e3`) -- absent from any database that branch never touched.
_ORPHANED_AGENT_EXECUTION_COLUMNS = (
    'total_tokens',
    'completion_tokens',
    'prompt_tokens',
    'model_used',
)


def upgrade() -> None:
    # This migration exists ONLY to undo drift the never-merged
    # `origin/simran-ekip` branch left on one specific already-diverged
    # database (see module docstring). On any database that branch never
    # touched -- a freshly created one for CI or local dev included -- none
    # of these objects exist, so every step here must be a safe no-op or
    # `alembic upgrade head` cannot complete at all. `DROP ... IF EXISTS`
    # covers the policies, indexes and tables; the RLS `ALTER`s and the
    # `DROP COLUMN`s have no `IF EXISTS` form of their own and are guarded
    # explicitly below (matching the `DO $$ ... IF ... $$` idempotency style
    # `b8f3d6a1c4e7` already uses for its own re-runnable role provisioning).
    for table in _RLS_TABLES:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF to_regclass('public.{table}') IS NOT NULL THEN
                    DROP POLICY IF EXISTS {_POLICY_NAME} ON {table};
                    ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
                    ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
                END IF;
            END
            $$;
            """
        )

    op.drop_index('ix_eval_case_results_org_id', table_name='eval_case_results', if_exists=True)
    op.drop_index('ix_eval_case_results_run_id', table_name='eval_case_results', if_exists=True)
    op.drop_table('eval_case_results', if_exists=True)

    op.drop_index('ix_eval_runs_org_started_at', table_name='eval_runs', if_exists=True)
    op.drop_table('eval_runs', if_exists=True)

    for column in _ORPHANED_AGENT_EXECUTION_COLUMNS:
        op.execute(f'ALTER TABLE agent_executions DROP COLUMN IF EXISTS {column}')


def downgrade() -> None:
    """Recreates exactly what `d8a2f6c1b9e3`/`d4f7b2e9c6a3` (the
    `origin/simran-ekip` branch) originally defined -- this migration's own
    reversal, not a resurrection of the lost/never-committed `b3d8f1a6c9e2`
    merge revision (see this file's own module docstring: that revision's
    exact contents were never seen and are not reproduced here on principle,
    not merely by omission).

    Not guarded with `IF NOT EXISTS` the way `upgrade()` now guards its
    drops: `downgrade` is only ever reached after a successful `upgrade()`
    in the same chain, at which point these objects are guaranteed absent.
    Running it against a database still carrying the branch objects (only
    possible by hand, off the alembic chain) will fail on the first
    `add_column` -- deliberately, rather than silently converging a
    hand-edited database to a state alembic never put it in.
    """
    op.add_column('agent_executions', sa.Column('model_used', sa.Text(), nullable=True))
    op.add_column('agent_executions', sa.Column('prompt_tokens', sa.Integer(), nullable=True))
    op.add_column('agent_executions', sa.Column('completion_tokens', sa.Integer(), nullable=True))
    op.add_column('agent_executions', sa.Column('total_tokens', sa.Integer(), nullable=True))

    op.create_table(
        'eval_runs',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('model_used', sa.Text(), nullable=False),
        sa.Column('git_commit', sa.Text(), nullable=True),
        sa.Column('case_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('passed_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('hallucination_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('avg_relevance_score', sa.Numeric(), nullable=True),
        sa.Column('avg_citation_accuracy_score', sa.Numeric(), nullable=True),
        sa.Column('avg_confidence_score', sa.Numeric(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False, server_default='running'),
        sa.Column('error_detail', sa.Text(), nullable=True),
        sa.Column(
            'started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_eval_runs_org_started_at', 'eval_runs', ['organization_id', 'started_at'],
        unique=False,
    )

    op.create_table(
        'eval_case_results',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('eval_run_id', sa.UUID(), nullable=False),
        sa.Column('case_id', sa.Text(), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('route_taken', sa.Text(), nullable=True),
        sa.Column('confidence_score', sa.Numeric(), nullable=True),
        sa.Column('citation_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('expected_sources', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('actual_sources', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('relevance_score', sa.Integer(), nullable=True),
        sa.Column('citation_accuracy_score', sa.Integer(), nullable=True),
        sa.Column('completeness_score', sa.Integer(), nullable=True),
        sa.Column('grounded', sa.Boolean(), nullable=True),
        sa.Column('hallucination_flag', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('passed', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('judge_reasoning', sa.Text(), nullable=True),
        sa.Column('error_detail', sa.Text(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['eval_run_id'], ['eval_runs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_eval_case_results_run_id', 'eval_case_results', ['eval_run_id'], unique=False,
    )
    op.create_index(
        'ix_eval_case_results_org_id', 'eval_case_results', ['organization_id'], unique=False,
    )

    for table in _RLS_TABLES:
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY {_POLICY_NAME} ON {table} USING ({_direct_using_clause()})'
        )
