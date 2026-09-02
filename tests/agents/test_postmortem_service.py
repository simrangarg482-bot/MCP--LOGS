"""`generate_postmortem` / `run_postmortem_pipeline` failure handling.

The MCP follow-up audit (2026-09-02) found `generate_postmortem` was the
one agent entry point that crashed outright on an LLM provider outage,
where `ask_question` / `investigate_incident` degrade. These tests pin the
fix: the pipeline's LLM steps retry (`agents.retry.call_with_retry`), and a
transient failure that outlasts the retries is re-raised as a typed
`ServiceUnavailableError` (503, "retry later"), never a generic crash --
while still never fabricating a `Postmortem`.
"""

from __future__ import annotations

import uuid

import openai
import pytest

from app.agents import retry as retry_module
from app.agents import service as agents_service
from app.agents.postmortem import pipeline as pipeline_module
from app.agents.postmortem.pipeline import run_postmortem_pipeline
from app.core.exceptions import NotFoundError, ServiceUnavailableError
from app.shared.schemas import Identity


class _FakeExecutionRow:
    def __init__(self, execution_id: uuid.UUID) -> None:
        self.id = execution_id


def _connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=None)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    """Keep the real `agents.retry.call_with_retry` in the path (these tests
    are about its integration with the postmortem pipeline) but strip its
    wall-clock backoff so a 3-attempt exhaustion is instant.
    """
    monkeypatch.setattr(retry_module, "full_jitter_backoff_seconds", lambda *a, **k: 0.0)


# --------------------------------------------------------------------------
# run_postmortem_pipeline -- retry behaviour
# --------------------------------------------------------------------------


async def test_pipeline_retries_a_transient_llm_failure_then_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(pipeline_module, "build_narrative", lambda entries: "timeline text")
    monkeypatch.setattr(pipeline_module, "latest_investigation_hypotheses", lambda entries: [])

    root_cause_calls = 0

    async def flaky_extract_root_cause(llm, narrative, hypotheses):
        nonlocal root_cause_calls
        root_cause_calls += 1
        if root_cause_calls == 1:
            raise _connection_error()
        return "The checkout adapter dereferenced a null response."

    async def ok_action_items(llm, narrative, root_cause):
        return []

    monkeypatch.setattr(pipeline_module, "extract_root_cause", flaky_extract_root_cause)
    monkeypatch.setattr(pipeline_module, "generate_action_items", ok_action_items)

    root_cause, action_items = await run_postmortem_pipeline(llm=object(), timeline_entries=[])

    assert root_cause == "The checkout adapter dereferenced a null response."
    assert action_items == []
    assert root_cause_calls == 2  # failed once, retried, succeeded


async def test_pipeline_propagates_the_transient_error_after_retries_exhausted(monkeypatch) -> None:
    monkeypatch.setattr(pipeline_module, "build_narrative", lambda entries: "timeline text")
    monkeypatch.setattr(pipeline_module, "latest_investigation_hypotheses", lambda entries: [])

    attempts = 0

    async def always_down(llm, narrative, hypotheses):
        nonlocal attempts
        attempts += 1
        raise _connection_error()

    monkeypatch.setattr(pipeline_module, "extract_root_cause", always_down)

    with pytest.raises(openai.APIConnectionError):
        await run_postmortem_pipeline(llm=object(), timeline_entries=[])

    assert attempts == 3  # initial + 2 retries (agents.retry._MAX_RETRIES)


# --------------------------------------------------------------------------
# generate_postmortem -- service-level classification
# --------------------------------------------------------------------------


async def test_generate_postmortem_maps_transient_llm_outage_to_503(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    actor = Identity.for_agent("postmortem_agent", organization_id)
    recorded: dict[str, object] = {}

    async def no_cost_budget(session, org_id):
        return None

    async def fake_insert(session, **kwargs):
        return _FakeExecutionRow(uuid.uuid4())

    async def fake_update(session, exec_id, **kwargs):
        recorded["update"] = kwargs

    async def fake_get_timeline(session, actor_, org_id, incident_id):
        return []

    async def down_pipeline(llm, timeline_entries):
        raise _connection_error()

    monkeypatch.setattr(agents_service.cost_budget, "check_cost_budget", no_cost_budget)
    monkeypatch.setattr(agents_service.repository, "insert_agent_execution", fake_insert)
    monkeypatch.setattr(agents_service.repository, "update_agent_execution", fake_update)
    monkeypatch.setattr(agents_service.incidents_service, "get_timeline", fake_get_timeline)
    monkeypatch.setattr(agents_service, "run_postmortem_pipeline", down_pipeline)

    with pytest.raises(ServiceUnavailableError) as excinfo:
        await agents_service.generate_postmortem(None, uuid.uuid4(), actor)

    assert excinfo.value.status_hint == 503
    assert excinfo.value.error_code == "agents.llm_unavailable"
    # still recorded the execution as failed -- never a fabricated success
    assert recorded["update"]["status"] == "failed"


async def test_generate_postmortem_still_propagates_a_real_bug_unchanged(monkeypatch) -> None:
    actor = Identity.for_agent("postmortem_agent", uuid.uuid4())
    recorded: dict[str, object] = {}

    async def no_cost_budget(session, org_id):
        return None

    async def fake_insert(session, **kwargs):
        return _FakeExecutionRow(uuid.uuid4())

    async def fake_update(session, exec_id, **kwargs):
        recorded["update"] = kwargs

    async def fake_get_timeline(session, actor_, org_id, incident_id):
        return []

    async def buggy_pipeline(llm, timeline_entries):
        raise KeyError("some dict key that should have been there")

    monkeypatch.setattr(agents_service.cost_budget, "check_cost_budget", no_cost_budget)
    monkeypatch.setattr(agents_service.repository, "insert_agent_execution", fake_insert)
    monkeypatch.setattr(agents_service.repository, "update_agent_execution", fake_update)
    monkeypatch.setattr(agents_service.incidents_service, "get_timeline", fake_get_timeline)
    monkeypatch.setattr(agents_service, "run_postmortem_pipeline", buggy_pipeline)

    with pytest.raises(KeyError):
        await agents_service.generate_postmortem(None, uuid.uuid4(), actor)

    assert recorded["update"]["status"] == "failed"


async def test_generate_postmortem_still_propagates_a_bad_incident_id(monkeypatch) -> None:
    actor = Identity.for_agent("postmortem_agent", uuid.uuid4())

    async def no_cost_budget(session, org_id):
        return None

    async def fake_insert(session, **kwargs):
        return _FakeExecutionRow(uuid.uuid4())

    async def fake_update(session, exec_id, **kwargs):
        return None

    async def missing_incident(session, actor_, org_id, incident_id):
        raise NotFoundError("Incident not found.", error_code="incident.not_found")

    monkeypatch.setattr(agents_service.cost_budget, "check_cost_budget", no_cost_budget)
    monkeypatch.setattr(agents_service.repository, "insert_agent_execution", fake_insert)
    monkeypatch.setattr(agents_service.repository, "update_agent_execution", fake_update)
    monkeypatch.setattr(agents_service.incidents_service, "get_timeline", missing_incident)

    with pytest.raises(NotFoundError):
        await agents_service.generate_postmortem(None, uuid.uuid4(), actor)
