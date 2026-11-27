"""sso configuration enabled flag

Revision ID: a1d4e7c2b9f3
Revises: f4b8d1c7a3e9
Create Date: 2026-11-26 12:00:00.000000

Adds `sso_configurations.enabled` so an organization administrator can switch
single sign-on off (and back on) without deleting the configuration.

* `enabled boolean NOT NULL DEFAULT true` -- a constant default, so on
  PostgreSQL 11+ this is a metadata-only change that rewrites no rows. Every
  existing configuration keeps working exactly as before (it becomes `true`).
* No existing value, secret, issuer or identity mapping is touched.
* `sso_configurations` already has row-level security (`c7d4e8f19a2b`); the new
  column is covered by the same `tenant_isolation` policy and needs no grant
  change (the application role already holds table-level privileges).

Downgrade drops the column only.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1d4e7c2b9f3'
down_revision: str | None = 'f4b8d1c7a3e9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'sso_configurations',
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
    )


def downgrade() -> None:
    op.drop_column('sso_configurations', 'enabled')
