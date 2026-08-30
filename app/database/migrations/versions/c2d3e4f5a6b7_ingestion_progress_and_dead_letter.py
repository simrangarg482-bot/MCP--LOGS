"""ingestion progress and dead-letter observability

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column_name in (
        "pages_fetched",
        "items_discovered",
        "items_skipped",
        "chunks_embedded",
        "retry_count",
    ):
        op.add_column(
            "ingestion_jobs",
            sa.Column(column_name, sa.Integer(), nullable=False, server_default="0"),
        )
    op.add_column(
        "ingestion_jobs",
        sa.Column("last_error_type", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ingestion_jobs", "last_error_type")
    for column_name in reversed(
        (
            "pages_fetched",
            "items_discovered",
            "items_skipped",
            "chunks_embedded",
            "retry_count",
        )
    ):
        op.drop_column("ingestion_jobs", column_name)
