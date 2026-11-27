"""SCIM 2.0 provisioning: per-organization configuration, bearer tokens, SCIM identities, held actions

Revision ID: b9f4d6e2a1c8
Revises: a8e3c5d1f2b7
Create Date: 2026-11-28 12:00:00.000000

Phase 2 of automated user offboarding. Additive: no existing table, column or row is changed.

* `scim_configurations` -- one row per organization that has ever opened Settings -> Provisioning:
  `enabled` (OFF by default), the mass-deactivation guard settings, the settle time for re-enables, and
  health fields (`last_request_at`, `last_success_at`, `last_error_*`).
* `scim_tokens` -- organization-specific opaque bearer tokens. **Only the SHA-256 hash is stored**; the raw
  value is shown once at creation. Expiry is mandatory, revocation is a timestamp, and several tokens may be
  active at once so a credential can be rotated with an overlap.
* `scim_identities` -- what the identity provider has told us about each user (userName, externalId, active).
  It is NOT an EKIP account and grants nothing: `user_id` stays NULL (a *pending* identity) until the
  identity is linked to an existing member by PROOF (the IdP's externalId equals the subject EKIP already
  verified through OIDC for that member) or by an administrator. Email never links anything on its own.
* `scim_pending_actions` -- deactivations held by the mass-deactivation guard until an administrator
  decides. At most one pending action per identity.
* `scim_authenticate_token(hash)` -- a narrow SECURITY DEFINER lookup (same pattern as
  `resolve_refresh_token_organization`): a bearer token arrives before any tenant context exists, so row-level
  security would hide every row from it. Returns only ids and the token's expiry / revocation. Fixed
  `search_path`; EXECUTE revoked from PUBLIC, granted to `ekip_app`.

Every table has `FORCE ROW LEVEL SECURITY` with the same `tenant_isolation` policy as the other tenant tables.

Downgrade drops the function and the four tables. Nothing else is touched.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'b9f4d6e2a1c8'
down_revision: str | None = 'a8e3c5d1f2b7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ('scim_configurations', 'scim_tokens', 'scim_identities', 'scim_pending_actions')

_AUTH_FN = """
CREATE FUNCTION public.scim_authenticate_token(p_token_hash text)
RETURNS TABLE (out_token_id uuid, out_organization_id uuid, out_expires_at timestamptz, out_revoked_at timestamptz)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT t.id, t.organization_id, t.expires_at, t.revoked_at
      FROM public.scim_tokens t
     WHERE t.token_hash = p_token_hash;
$$;
"""

_APP_ROLE = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ekip_app') THEN
        GRANT EXECUTE ON FUNCTION public.scim_authenticate_token(text) TO ekip_app;
    END IF;
END
$$;
"""


def _uuid_pk():
    return sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False)


def _now(name: str, nullable: bool = False):
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.text('now()') if not nullable else None, nullable=nullable)


def upgrade() -> None:
    op.create_table(
        'scim_configurations',
        sa.Column('organization_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('guard_min_absolute', sa.Integer(), server_default=sa.text('5'), nullable=False),
        sa.Column('guard_percent', sa.Integer(), server_default=sa.text('20'), nullable=False),
        sa.Column('guard_window_minutes', sa.Integer(), server_default=sa.text('60'), nullable=False),
        sa.Column('reactivation_settle_seconds', sa.Integer(), server_default=sa.text('300'), nullable=False),
        _now('last_request_at', nullable=True),
        _now('last_success_at', nullable=True),
        sa.Column('last_error_code', sa.Text(), nullable=True),
        _now('last_error_at', nullable=True),
        _now('created_at'),
        _now('updated_at'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('organization_id'),
        sa.CheckConstraint('guard_min_absolute >= 1', name='ck_scim_cfg_guard_min'),
        sa.CheckConstraint('guard_percent BETWEEN 1 AND 100', name='ck_scim_cfg_guard_percent'),
        sa.CheckConstraint('guard_window_minutes BETWEEN 1 AND 1440', name='ck_scim_cfg_guard_window'),
        sa.CheckConstraint('reactivation_settle_seconds >= 0', name='ck_scim_cfg_settle'),
    )

    op.create_table(
        'scim_tokens',
        _uuid_pk(),
        sa.Column('organization_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('token_hash', sa.Text(), nullable=False),
        sa.Column('token_prefix', sa.Text(), nullable=False),
        sa.Column('created_by', postgresql.UUID(as_uuid=True), nullable=True),
        _now('created_at'),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        _now('last_used_at', nullable=True),
        _now('revoked_at', nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash', name='uq_scim_tokens_hash'),
    )
    op.create_index('ix_scim_tokens_org', 'scim_tokens', ['organization_id'])

    op.create_table(
        'scim_identities',
        _uuid_pk(),
        sa.Column('organization_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_name', sa.Text(), nullable=False),
        sa.Column('user_name_key', sa.Text(), nullable=False),
        sa.Column('external_id', sa.Text(), nullable=True),
        sa.Column('display_name', sa.Text(), nullable=True),
        sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('linked_via', sa.Text(), nullable=True),
        _now('linked_at', nullable=True),
        _now('deleted_at', nullable=True),
        _now('created_at'),
        _now('updated_at'),
        _now('last_seen_at'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("linked_via IS NULL OR linked_via IN ('sso_subject', 'admin')", name='ck_scim_identities_via'),
    )
    op.create_index(
        'uq_scim_identities_username', 'scim_identities', ['organization_id', 'user_name_key'],
        unique=True, postgresql_where=sa.text('deleted_at IS NULL'),
    )
    op.create_index(
        'uq_scim_identities_external', 'scim_identities', ['organization_id', 'external_id'],
        unique=True, postgresql_where=sa.text('external_id IS NOT NULL AND deleted_at IS NULL'),
    )
    op.create_index(
        'uq_scim_identities_user', 'scim_identities', ['organization_id', 'user_id'],
        unique=True, postgresql_where=sa.text('user_id IS NOT NULL AND deleted_at IS NULL'),
    )

    op.create_table(
        'scim_pending_actions',
        _uuid_pk(),
        sa.Column('organization_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('scim_identity_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('action', sa.Text(), server_default='deactivate', nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), server_default='pending', nullable=False),
        _now('created_at'),
        sa.Column('decided_by', postgresql.UUID(as_uuid=True), nullable=True),
        _now('decided_at', nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['scim_identity_id'], ['scim_identities.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['decided_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("action IN ('deactivate')", name='ck_scim_pending_action'),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected')", name='ck_scim_pending_status'),
    )
    op.create_index(
        'uq_scim_pending_identity', 'scim_pending_actions', ['organization_id', 'scim_identity_id'],
        unique=True, postgresql_where=sa.text("status = 'pending'"),
    )

    for table in _TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON public.{table} "
            "USING (organization_id = current_setting('app.current_organization_id', true)::uuid)"
        )

    op.execute(_AUTH_FN)
    op.execute("REVOKE ALL ON FUNCTION public.scim_authenticate_token(text) FROM PUBLIC")
    op.execute(_APP_ROLE)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.scim_authenticate_token(text)")
    op.drop_index('uq_scim_pending_identity', table_name='scim_pending_actions')
    op.drop_table('scim_pending_actions')
    op.drop_index('uq_scim_identities_user', table_name='scim_identities')
    op.drop_index('uq_scim_identities_external', table_name='scim_identities')
    op.drop_index('uq_scim_identities_username', table_name='scim_identities')
    op.drop_table('scim_identities')
    op.drop_index('ix_scim_tokens_org', table_name='scim_tokens')
    op.drop_table('scim_tokens')
    op.drop_table('scim_configurations')
