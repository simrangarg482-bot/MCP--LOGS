"""password reset links and users.sessions_valid_after

Revision ID: b2c5e8a1d4f7
Revises: a1d4e7c2b9f3
Create Date: 2026-11-26 15:00:00.000000

* `password_resets` -- outstanding "forgot password" links (SHA-256 token hash,
  expiry, single-use `consumed_at`, send bookkeeping). At most one OPEN link per
  user (partial unique index). Global like `users`/`email_verifications`: no
  organization column, no row-level security (a reset happens before any
  session exists and a password spans every organization); `ekip_app` already
  has DML on it through the default privileges from `b8f3d6a1c4e7`.
* `users.sessions_valid_after` -- nullable timestamptz. NULL for everyone today:
  no existing access token is affected by this migration. A password reset or
  change sets it, and every access token issued before it is then refused.

* `revoke_user_refresh_tokens(user_id, revoked_at)` -- narrow SECURITY DEFINER
  function (same pattern and `search_path` pinning as `d2e5f8a3c1b6`). A password
  reset must end the person's sessions in EVERY organization, including one they
  were since removed from, and `refresh_tokens` has FORCE row-level security: no
  tenant-scoped query can reach tokens of other organizations. The function can
  only set `revoked_at` on that user's still-valid tokens -- it reads nothing back
  but a count. EXECUTE is limited to the application role.

Purely additive; no existing row or value is rewritten. Downgrade drops all three.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b2c5e8a1d4f7'
down_revision: str | None = 'a1d4e7c2b9f3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('users', sa.Column('sessions_valid_after', sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        'password_resets',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('token_hash', sa.Text(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('send_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('send_window_started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )
    op.create_index(
        'uq_password_resets_open_user',
        'password_resets',
        ['user_id'],
        unique=True,
        postgresql_where=sa.text('consumed_at IS NULL'),
    )
    op.create_index('ix_password_resets_expires_at', 'password_resets', ['expires_at'])
    op.execute(
        """
        CREATE FUNCTION revoke_user_refresh_tokens(p_user_id uuid, p_revoked_at timestamptz)
        RETURNS integer
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            WITH revoked AS (
                UPDATE refresh_tokens SET revoked_at = p_revoked_at
                WHERE user_id = p_user_id AND revoked_at IS NULL
                RETURNING 1
            )
            SELECT count(*)::integer FROM revoked;
        $$;
        """
    )
    op.execute('REVOKE ALL ON FUNCTION revoke_user_refresh_tokens(uuid, timestamptz) FROM PUBLIC')
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ekip_app') THEN
                GRANT EXECUTE ON FUNCTION revoke_user_refresh_tokens(uuid, timestamptz) TO ekip_app;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute('DROP FUNCTION IF EXISTS revoke_user_refresh_tokens(uuid, timestamptz)')
    op.drop_index('ix_password_resets_expires_at', table_name='password_resets')
    op.drop_index('uq_password_resets_open_user', table_name='password_resets')
    op.drop_table('password_resets')
    op.drop_column('users', 'sessions_valid_after')
