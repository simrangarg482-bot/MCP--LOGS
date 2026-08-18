"""add token_hash to invitations

Revision ID: 1269a7b553a9
Revises: f1ea4eb67264
Create Date: 2026-08-18 14:48:50.303690

Phase 7.5/7.6: closes a confirmed backend gap where `POST /invitations/{id}/
accept` accepted no proof the caller controls the invited email address --
the only "token" was the invitation's own database `id` (a UUID4 primary
key), which anyone who learned it (a leaked log line, another org member,
an admin API response) could use to permanently consume the invitation with
no proof of email ownership and, critically, without the accept flow ever
actually provisioning a user account (see `docs/operations/
invitation-flow.md` for the full before/after).

Nullable, not backfilled: an existing `pending` invitation created before
this migration has no real token and can never be validly accepted through
the new token-checked path -- correct, fail-closed behavior for a
half-provisioned invitation whose only prior "security" was an
unauthenticated database id. Revoke and re-send any such invitation instead
of trying to backfill a token retroactively (there would be no way to
communicate a backfilled token to the invited email address after the fact
anyway).
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1269a7b553a9'
down_revision: str | None = 'f1ea4eb67264'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('invitations', sa.Column('token_hash', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('invitations', 'token_hash')
