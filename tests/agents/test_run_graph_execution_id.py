"""`_run_graph_and_record` hands the caller the id of the execution that
produced the response (so feedback attaches to exactly that run), records
non-sensitive output facts, and withholds the id when there is no answer to
rate. Same fake-graph style as `test_run_graph_and_record_telemetry.py`."""

from __future__ import annotations

import uuid

import pytest

from app.agents import service as agents_service
from app.agents.graph import GraphState
from app.shared.schemas import ActorKind, AskResponse, Citation, Identity


def _actor() -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )


class _Graph:
    def __init__(self, final_state=None, error: Exception | None = None) -> None:
        self._final_state = final_state
        self._error = error

    async def ainvoke(self, initial_state, config=None):
        if self._error is not None:
            raise self._error
        return self._final_state


@pytest.fixture()
def recorded(monkeypatch):
    """Capture inserts/updates; every insert gets a fresh, known id."""
    inserted_ids: list[uuid.UUID] = []
    updates: list[dict] = []

    async def fake_insert(session, **kwargs):
        execution_id = uuid.uuid4()
        inserted_ids.append(execution_id)
        return type("Row", (), {"id": execution_id})()

    async def fake_update(session, execution_id, **kwargs):
        updates.append({"id": execution_id, **kwargs})

    async def no_budget_check(session, organization_id):
        return None

    monkeypatch.setattr(agents_service.repository, "insert_agent_execution", fake_insert)
    monkeypatch.setattr(agents_service.repository, "update_agent_execution", fake_update)
    monkeypatch.setattr(agents_service.cost_budget, "check_cost_budget", no_budget_check)
    return inserted_ids, updates


async def _run(graph, actor):
    return await agents_service._run_graph_and_record(
        None,
        agent_name="answer_question",
        trigger_source="core_api",
        input_summary={"query": "why did checkout break?"},
        graph=graph,
        initial_state=GraphState(query="why did checkout break?", actor=actor),
        fallback_route="answer",
    )


def _citation(document_id: uuid.UUID) -> Citation:
    return Citation(document_id=document_id, chunk_id=uuid.uuid4(), source_url=None, excerpt="SECRET EXCERPT")


@pytest.mark.asyncio
async def test_successful_response_carries_the_executions_own_id(recorded) -> None:
    inserted_ids, updates = recorded
    actor = _actor()
    state = GraphState(query="q", actor=actor)
    state.result = AskResponse(confidence=0.9, route_taken="answer", answer="Because X.", answer_mode="answered")
    state.confidence_score = 0.9

    result = await _run(_Graph(state), actor)

    assert result.execution_id == inserted_ids[0]
    assert updates[-1]["id"] == inserted_ids[0]  # the same row that was marked succeeded
    assert result.answer == "Because X."  # nothing else about the response changed


@pytest.mark.asyncio
async def test_each_run_gets_its_own_execution_id(recorded) -> None:
    """Repeated questions are separate executions; feedback must not be shared."""
    actor = _actor()

    def state():
        s = GraphState(query="q", actor=actor)
        s.result = AskResponse(confidence=0.9, route_taken="answer", answer="A", answer_mode="answered")
        return s

    first = await _run(_Graph(state()), actor)
    second = await _run(_Graph(state()), actor)

    assert first.execution_id != second.execution_id


@pytest.mark.asyncio
async def test_output_summary_records_ids_and_counts_never_content(recorded) -> None:
    _, updates = recorded
    actor = _actor()
    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
    state = GraphState(query="q", actor=actor)
    state.result = AskResponse(
        confidence=0.8,
        route_taken="answer",
        answer="A confidential answer body.",
        answer_mode="answered",
        citations=[_citation(doc_a), _citation(doc_a), _citation(doc_b)],
    )

    await _run(_Graph(state), actor)

    summary = updates[-1]["output_summary"]
    assert summary == {
        "route_taken": "answer",
        "answer_mode": "answered",
        "citation_count": 3,
        "cited_document_ids": sorted([str(doc_a), str(doc_b)]),
    }
    assert "confidential" not in repr(summary)
    assert "SECRET EXCERPT" not in repr(summary)


@pytest.mark.asyncio
async def test_unexpected_failure_returns_no_execution_id(recorded) -> None:
    _, updates = recorded

    result = await _run(_Graph(error=RuntimeError("boom")), _actor())

    assert result.execution_id is None  # a generic error message is not an answer to rate
    assert updates[-1]["status"] == "failed"
    assert "output_summary" not in updates[-1]


@pytest.mark.asyncio
async def test_graph_without_a_result_returns_no_execution_id(recorded) -> None:
    actor = _actor()
    state = GraphState(query="q", actor=actor)  # result stays None

    result = await _run(_Graph(state), actor)

    assert result.execution_id is None


def test_execution_id_is_optional_so_existing_constructors_still_work() -> None:
    response = AskResponse(confidence=0.5, route_taken="answer", answer="x")

    assert response.execution_id is None
    assert "execution_id" in response.model_dump(mode="json")  # present, null
