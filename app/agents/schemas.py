"""Pydantic contracts local to agents/ -- not shared cross-module (contrast
`app.shared.schemas.agent_contracts`, which holds the types `agents/`
produces *for* other modules to consume).

Owned by: agents/. `AgentExecution` is the read-side view of one
`agent_executions` row (`app.database.models.agent_models.AgentExecution`),
same name-mirrors-the-ORM-row convention `ingestion.schemas.IngestionJob`
already uses for its own job table -- distinguished by import path, not by a
different name.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.shared.schemas import AgentExecutionStatus


class AgentExecution(BaseModel):
    """One row of `agent_executions`, as read back after
    `repository.insert_agent_execution`/`update_agent_execution`."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    agent_name: str
    trigger_source: str
    input_summary: dict | None
    confidence_score: float | None
    status: AgentExecutionStatus
    error_detail: str | None
    started_at: datetime
    completed_at: datetime | None


class AgentExecutionStats(BaseModel):
    """Aggregated execution metrics for one agent, over some organization
    and time window -- Milestone 10's observability-dashboard requirement
    (PROJECT_PLAN.md section 10), the `agent_executions`-side counterpart to
    `core.observability.schemas.McpToolStats`. Organization-scoped (unlike
    `McpToolStats`): `agent_executions.organization_id` exists specifically
    because a *named* per-tenant consumer (the Knowledge Gap Agent) already
    needs it -- see `app.database.models.agent_models.AgentExecution`'s own
    docstring -- so a per-tenant dashboard view is a natural, already-
    supported query shape here, unlike `mcp_requests`.
    """

    model_config = ConfigDict(frozen=True)

    agent_name: str
    execution_count: int
    succeeded_count: int
    failed_count: int
    avg_confidence_score: float | None
    avg_latency_seconds: float | None
    #: Phase 5.7 -- `None` (not `0`) whenever no execution in this group
    #: captured usage at all (see `AgentExecution`'s own column comments on
    #: why absence and zero are kept distinct throughout this feature).
    total_prompt_tokens: int | None
    total_completion_tokens: int | None
    total_tokens: int | None
    #: An estimate from published pricing, not real billing data -- see
    #: `app.agents.telemetry.get_estimated_cost_usd`'s own docstring.
    estimated_cost_usd: float | None
