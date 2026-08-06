"""Tests for `app.agents.workers.tasks.run_knowledge_gap_detection_task`'s
Milestone 10 RLS wiring -- unlike `app.ingestion.service._execute_ingestion_job`,
this task already knows its `organization_id` from its own arq job argument,
so it can call `set_tenant_context` immediately, with no bypass-function
lookup needed first.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest

from app.agents.workers import tasks as tasks_module


class _FakeSession:
    """`run_knowledge_gap_detection_task` never queries this session
    directly -- only passes it through to `set_tenant_context` and
    `agents_service.detect_knowledge_gaps` (both monkeypatched below).
    """


@pytest.mark.asyncio
async def test_sets_tenant_context_before_detecting_knowledge_gaps(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    session = _FakeSession()
    call_order: list[str] = []

    @asynccontextmanager
    async def fake_session_scope():
        yield session

    async def fake_set_tenant_context(session_arg, org_id) -> None:
        call_order.append("set_tenant_context")
        assert session_arg is session
        assert org_id == organization_id

    async def fake_detect_knowledge_gaps(session_arg, actor):
        call_order.append("detect_knowledge_gaps")
        assert session_arg is session
        assert actor.organization_id == organization_id
        return []

    monkeypatch.setattr(tasks_module, "session_scope", fake_session_scope)
    monkeypatch.setattr(tasks_module, "set_tenant_context", fake_set_tenant_context)
    monkeypatch.setattr(
        tasks_module.agents_service, "detect_knowledge_gaps", fake_detect_knowledge_gaps
    )

    await tasks_module.run_knowledge_gap_detection_task({}, str(organization_id))

    assert call_order == ["set_tenant_context", "detect_knowledge_gaps"]
