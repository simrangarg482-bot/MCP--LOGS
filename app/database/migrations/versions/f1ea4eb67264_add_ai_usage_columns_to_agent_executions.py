"""add AI usage/cost telemetry columns to agent_executions

Revision ID: f1ea4eb67264
Revises: d706a360fc2a
Create Date: 2026-08-18 13:10:23.993786

Phase 5.4/5.7 (AI usage/cost telemetry): adds `model_used`, `prompt_tokens`,
`completion_tokens`, `total_tokens` to `agent_executions`, populated by
`app.agents.service._run_graph_and_record`/`generate_postmortem`/
`detect_knowledge_gaps` via `app.agents.telemetry.summarize_usage`
(LangChain's `UsageMetadataCallbackHandler`, attached per execution).

Note on provenance: an earlier, never-merged branch
(`origin/simran-ekip`'s `d8a2f6c1b9e3`) added nearly-identical columns to
this same table for a similar purpose, and left them physically present
(but never wired into any application code) on the shared Neon development
database -- see `docs/operations/migration-recovery.md` for the full
investigation. This migration is a fresh, independent authorship on `main`'s
own chain, not a resurrection of that file, written because Phase 5 of this
project now has a real, tested code path that populates these columns --
the previous instance never did. `docs/operations/migration-recovery.md`'s
Option 3 recovery migration (`90ff736ced55`) drops that branch's version of
these columns from Neon; this migration adds `main`'s own version back,
under `main`'s own authority, once there is real code behind it.

All four columns are nullable: `NULL` means "not captured" (a pre-Phase-5
execution, or one whose LLM call never returned `usage_metadata`), never
"zero tokens spent" -- see `app.database.models.agent_models.AgentExecution`'s
own column comments.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1ea4eb67264'
down_revision: str | None = 'd706a360fc2a'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('agent_executions', sa.Column('model_used', sa.Text(), nullable=True))
    op.add_column('agent_executions', sa.Column('prompt_tokens', sa.Integer(), nullable=True))
    op.add_column('agent_executions', sa.Column('completion_tokens', sa.Integer(), nullable=True))
    op.add_column('agent_executions', sa.Column('total_tokens', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('agent_executions', 'total_tokens')
    op.drop_column('agent_executions', 'completion_tokens')
    op.drop_column('agent_executions', 'prompt_tokens')
    op.drop_column('agent_executions', 'model_used')
