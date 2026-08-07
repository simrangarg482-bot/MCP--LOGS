"""add mcp_requests table

Revision ID: e3f6a1b8d4c9
Revises: d2e5f8a3c1b6
Create Date: 2026-08-06 00:00:00.000000

`app.database.models.mcp_models.McpRequest` has existed since Stage S/File 20
of the code-reading roadmap, but no prior migration ever ran a
`CREATE TABLE mcp_requests` -- an oversight the milestone 10 RLS migration's
own docstring papered over by only *explaining* why the table is excluded
from RLS, not confirming it exists. Every `core.observability.service.
record_mcp_request` write and `get_mcp_dashboard` read has been failing with
`UndefinedTableError` against any database that only ever ran
`alembic upgrade head` before now.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e3f6a1b8d4c9'
down_revision: str | None = 'd2e5f8a3c1b6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'mcp_requests',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('tool_name', sa.Text(), nullable=False),
        sa.Column('identity', sa.Text(), nullable=False),
        sa.Column('request_summary', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('status_code', sa.Integer(), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column(
            'occurred_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_mcp_requests_tool_name_occurred_at',
        'mcp_requests',
        ['tool_name', 'occurred_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_mcp_requests_tool_name_occurred_at', table_name='mcp_requests')
    op.drop_table('mcp_requests')
