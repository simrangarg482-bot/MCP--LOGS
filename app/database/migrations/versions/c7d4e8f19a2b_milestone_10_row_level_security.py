"""milestone 10: postgres row-level security (tenant isolation backstop)

Revision ID: c7d4e8f19a2b
Revises: a1c3e9f2b7d4
Create Date: 2026-08-05 00:00:00.000000

Closes the one real finding of EKIP_TENANT_ISOLATION_SECURITY_REVIEW.md:
every tenant-isolation guarantee this application had was enforced entirely
in application code (PROJECT_PLAN.md section 3.7's "Application queries"
row), with no database-level backstop underneath it, despite section 3.7's
own table listing RLS as a distinct enforcement layer and Milestone 2's
original scope explicitly calling for it.

Design, one paragraph: every table below gets `ENABLE ROW LEVEL SECURITY`
plus `FORCE ROW LEVEL SECURITY` (so the policy also applies to the table
owner -- the role this application's own connection pool uses -- not just
to other roles; RLS is still bypassed for an actual Postgres superuser
regardless of FORCE, so the deployed database user must NOT be a superuser
for this to mean anything, a requirement flagged again in the review doc's
recommendation #2) and one policy comparing the row's own `organization_id`
column against a session-local setting, `app.current_organization_id`,
that `app.database.session` sets via `SET LOCAL` at the start of every
request/job after `Identity` resolution (see that module's own docstring
for exactly where). `current_setting(..., true)` (the `true` = missing_ok
argument) returns `NULL`, not an error, if that setting was somehow never
set for a given connection -- and `organization_id = NULL` evaluates to
`NULL` (falsy) in every row, meaning a connection that forgot to set this
variable sees ZERO rows from any RLS-protected table, not every row. This
is deliberately fail-closed: a bug in the session-variable wiring produces
an obvious "nothing works" failure to notice and fix, never a silent
cross-tenant leak.

No explicit `FOR SELECT`/`FOR INSERT` scoping on any policy below -- a bare
`CREATE POLICY ... USING (...)` with no `FOR` clause applies to `FOR ALL`
(SELECT/INSERT/UPDATE/DELETE) using the same expression for both "which
existing rows are visible" and "is this new/updated row allowed" (Postgres
uses `USING` for existing rows and, absent a separate `WITH CHECK` clause,
also for validating new/changed rows) -- exactly the behavior wanted: this
also stops an application bug from inserting a row tagged with the *wrong*
organization_id, not just from reading across tenants.

Two tables need a subquery-based policy instead of a direct column compare,
because they carry no `organization_id` column of their own (scoped via a
foreign key to a table that does): `document_metadata` (via `document_id` ->
`documents.organization_id`) and `project_memberships` (via `project_id` ->
`projects.organization_id`).

`audit_logs.organization_id` is nullable (a pre-multi-tenancy column, per
that model's own docstring) -- the same direct-compare policy still applies
unchanged: a null-`organization_id` row simply becomes invisible under RLS
(the same `NULL = ...` -> `NULL` -> falsy behavior described above), not an
error and not a leak.

NOT covered by RLS, deliberately: `organizations` itself (the root of the
tenancy tree -- nothing to scope it *against*), `users`/`roles`/
`permissions`/`role_permissions` (either genuinely global catalogs or, for
`users`, deliberately organization-less by design -- see `core_models.py`'s
own docstring: "a user's company membership(s)... live on UserRole"), and
`mcp_requests` (deliberately carries no `organization_id` at all -- see that
model's own docstring).
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c7d4e8f19a2b'
down_revision: str | None = 'a1c3e9f2b7d4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUC_NAME = 'app.current_organization_id'

# Tables with their own, direct `organization_id` column.
_DIRECT_TABLES = [
    'projects',
    'sso_configurations',
    'external_identity_mappings',
    'connector_configs',
    'organization_access_rules',
    'invitations',
    'user_roles',
    'incidents',
    'incident_timeline',
    'postmortems',
    'audit_logs',
    'ingestion_jobs',
    'documents',
    'agent_executions',
    'knowledge_gap_reports',
    'refresh_tokens',
    'documentation_chunks',
    'code_chunks',
    'conversations_chunks',
]

# (table, parent_table, foreign_key_column) for tables scoped only via a
# join -- see module docstring.
_JOIN_SCOPED_TABLES = [
    ('document_metadata', 'documents', 'document_id'),
    ('project_memberships', 'projects', 'project_id'),
]

_POLICY_NAME = 'tenant_isolation'


def _direct_using_clause() -> str:
    return f"organization_id = current_setting('{_GUC_NAME}', true)::uuid"


def _join_using_clause(parent_table: str, fk_column: str) -> str:
    return (
        f"{fk_column} IN (SELECT id FROM {parent_table} "
        f"WHERE organization_id = current_setting('{_GUC_NAME}', true)::uuid)"
    )


def upgrade() -> None:
    for table in _DIRECT_TABLES:
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY {_POLICY_NAME} ON {table} USING ({_direct_using_clause()})'
        )

    for table, parent_table, fk_column in _JOIN_SCOPED_TABLES:
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY {_POLICY_NAME} ON {table} '
            f'USING ({_join_using_clause(parent_table, fk_column)})'
        )


def downgrade() -> None:
    for table in _DIRECT_TABLES:
        op.execute(f'DROP POLICY IF EXISTS {_POLICY_NAME} ON {table}')
        op.execute(f'ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} DISABLE ROW LEVEL SECURITY')

    for table, _parent_table, _fk_column in _JOIN_SCOPED_TABLES:
        op.execute(f'DROP POLICY IF EXISTS {_POLICY_NAME} ON {table}')
        op.execute(f'ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} DISABLE ROW LEVEL SECURITY')
