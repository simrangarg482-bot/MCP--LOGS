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
