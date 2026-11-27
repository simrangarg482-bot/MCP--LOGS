"""invitation email delivery tracking

Revision ID: f4b8d1c7a3e9
Revises: e8c3a1f5b7d2
Create Date: 2026-11-26 00:00:00.000000

Adds the columns the "email the invitation link" feature needs on
`invitations`:

* `email_status`  -- `not_sent` | `queued` | `sent` | `failed` | `not_configured`
  (CHECK-constrained). `sent` is written only after the SMTP provider accepted
  the message. Every existing row becomes `not_sent` (it was created before
  email delivery existed and was handed over by copying the link).
* `email_attempts`, `email_last_attempt_at`, `email_sent_at` -- delivery
  bookkeeping for the status badge and the resend cooldown.
* `resend_count` -- bounds how often one invitation can be re-sent.
* `ix_invitations_org_created` -- serves the per-organization hourly/daily
  invitation caps (a count over `created_at`).

Purely additive and safe on a live table: every new column is NOT NULL with a
constant default (a metadata-only change on PostgreSQL 11+), or nullable. No
existing value is rewritten. `invitations` already has row-level security
(`c7d4e8f19a2b`); nothing here changes its policy.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f4b8d1c7a3e9'
down_revision: str | None = 'e8c3a1f5b7d2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'invitations',
        sa.Column('email_status', sa.Text(), nullable=False, server_default=sa.text("'not_sent'")),
    )
    op.add_column(
        'invitations',
        sa.Column('email_attempts', sa.Integer(), nullable=False, server_default=sa.text('0')),
    )
    op.add_column('invitations', sa.Column('email_sent_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        'invitations', sa.Column('email_last_attempt_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        'invitations',
        sa.Column('resend_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
    )
    op.create_check_constraint(
        'ck_invitations_email_status',
        'invitations',
        "email_status IN ('not_sent', 'queued', 'sent', 'failed', 'not_configured')",
    )
    op.create_index(
        'ix_invitations_org_created', 'invitations', ['organization_id', 'created_at']
    )


def downgrade() -> None:
    op.drop_index('ix_invitations_org_created', table_name='invitations')
    op.drop_constraint('ck_invitations_email_status', 'invitations', type_='check')
    for column in ('resend_count', 'email_last_attempt_at', 'email_sent_at', 'email_attempts', 'email_status'):
        op.drop_column('invitations', column)
