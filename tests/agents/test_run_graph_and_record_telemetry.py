"""Tests for `app.agents.service._run_graph_and_record`'s Phase 5.4/5.7
token-usage capture -- proves the `UsageMetadataCallbackHandler` attached at
the top-level `graph.ainvoke` call actually flows through to the persisted
`agent_executions` row, using a fake graph that manipulates whatever handler
it's given via `config["callbacks"]`, the same shape LangChain's own runtime
would populate it in via `on_llm_end`.
"""

from __future__ import annotations

import uuid

import pytest

from app.agents import service as agents_service
from app.agents.graph import GraphState
from app.shared.schemas import ActorKind, AskResponse, Identity


def _actor(organization_id: uuid.UUID) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
    )


class _FakeGraphWithUsage:
    """Simulates a LangGraph `CompiledGraph.ainvoke` that made one real LLM
    call: populates `config["callbacks"][0].usage_metadata`, exactly what
    `UsageMetadataCallbackHandler.on_llm_end` does for a real response.
    """

    def __init__(self, final_state) -> None:
        self._final_state = final_state

    async def ainvoke(self, initial_state, config=None):
        handler = config["callbacks"][0]
        handler.usage_metadata["gpt-4o-mini"] = {
            "input_tokens": 42,
            "output_tokens": 8,
            "total_tokens": 50,
        }
        return self._final_state


@pytest.mark.asyncio
async def test_successful_execution_persists_captured_token_usage(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    actor = _actor(organization_id)
    response = AskResponse(confidence=0.9, route_taken="answer", answer="Because X.", citations=[])
    final_state = GraphState(query="why did checkout break?", actor=actor)
    final_state.result = response
    final_state.confidence_score = 0.9
    fake_graph = _FakeGraphWithUsage(final_state)

    inserted = {}
    updated = {}

    async def fake_insert(session, **kwargs):
        inserted.update(kwargs)
        return type("Row", (), {"id": uuid.uuid4()})()

    async def fake_update(session, execution_id, **kwargs):
        updated.update(kwargs)

    monkeypatch.setattr(agents_service.repository, "insert_agent_execution", fake_insert)
    monkeypatch.setattr(agents_service.repository, "update_agent_execution", fake_update)

    initial_state = GraphState(query="why did checkout break?", actor=actor)

    result = await agents_service._run_graph_and_record(
        None,
        agent_name="answer_question",
        trigger_source="core_api",
        input_summary={"query": "why did checkout break?"},
        graph=fake_graph,
        initial_state=initial_state,
        fallback_route="answer",
    )

    assert result.answer == "Because X."
    assert updated["status"] == "succeeded"
    assert updated["model_used"] == "gpt-4o-mini"
    assert updated["prompt_tokens"] == 42
    assert updated["completion_tokens"] == 8
    assert updated["total_tokens"] == 50


class _FakeGraphNoUsage:
    """Simulates a graph run whose LLM call never produced `usage_metadata`
    (e.g. every node call was itself mocked one level deeper) -- the
    `agent_executions` update must simply omit the token fields, never write
    them as `0`.
    """

    def __init__(self, final_state) -> None:
        self._final_state = final_state

    async def ainvoke(self, initial_state, config=None):
        return self._final_state


@pytest.mark.asyncio
async def test_execution_with_no_captured_usage_omits_token_fields(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    actor = _actor(organization_id)
    response = AskResponse(confidence=0.9, route_taken="answer", answer="Because X.", citations=[])
    final_state = GraphState(query="why did checkout break?", actor=actor)
    final_state.result = response
    final_state.confidence_score = 0.9
    fake_graph = _FakeGraphNoUsage(final_state)

    updated = {}

    async def fake_insert(session, **kwargs):
        return type("Row", (), {"id": uuid.uuid4()})()

    async def fake_update(session, execution_id, **kwargs):
        updated.update(kwargs)

    monkeypatch.setattr(agents_service.repository, "insert_agent_execution", fake_insert)
    monkeypatch.setattr(agents_service.repository, "update_agent_execution", fake_update)

    initial_state = GraphState(query="why did checkout break?", actor=actor)

    await agents_service._run_graph_and_record(
        None,
        agent_name="answer_question",
        trigger_source="core_api",
        input_summary={"query": "why did checkout break?"},
        graph=fake_graph,
        initial_state=initial_state,
        fallback_route="answer",
    )

    assert "model_used" not in updated
    assert "prompt_tokens" not in updated
    assert "total_tokens" not in updated
