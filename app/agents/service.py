"""Public interface for agents/ (PROJECT_PLAN.md section 9.7):
`answer_question`, `triage_incident` (task #23), and, as of task #24,
`generate_postmortem`. `detect_knowledge_gaps` (API_DESIGN.md section 2's
remaining entry point) is not implemented here: it depends on the Knowledge
Gap Agent, Milestone 9 -- stubbing it now would mean inventing behavior for
an agent that doesn't exist, rather than an honest "not yet built."

`generate_postmortem` returns *computed content only* (`(root_cause,
action_items)`), never a persisted row -- `core.incidents.service.
trigger_postmortem_generation` (task #25) is the actual glue that calls this
function and then persists the result via `core.incidents.service.
create_postmortem`. This mirrors `answer_question`/`triage_incident` in one
sense (agents/ never writes to a `core`-owned table directly) but not
another: those two return a fully-formed `AskResponse` because `AskResponse`
isn't itself a persisted row anywhere, whereas `Postmortem` is a row in a
table `core/incidents` owns and gates (`postmortem:write`,
`postmortem:approve`) -- so persisting it is deliberately kept on the
`core/incidents` side of the boundary, not delegated back into `agents/`.

Records one `agent_executions` row per call (DATABASE_DESIGN.md: "the data
source for the Knowledge Gap Agent"), transitioning `running` ->
`succeeded`/`failed` -- the same job-lifecycle shape as
`ingestion.service`'s `run_ingestion_job`. Like every other service in this
codebase, never commits its own session (core.audit.service's docstring on
why); the caller's `session_scope()`/`get_db_session` does that.

Failure handling per AGENT_WORKFLOWS.md section 4 distinguishes two cases:
- **Expected domain errors** (`app.core.exceptions.EKIPError` and its
  subclasses -- e.g. a bad `incident_id`, per `agents.retrieval.rewriting`'s
  own comment on why it doesn't catch these): marked `failed` for
  observability, then re-raised so a future REST/MCP boundary layer can map
  them to their real status code (404/403/...), exactly the behavior
  `EKIPError`'s own docstring describes ("propagate ... become a 500" would
  be *wrong* for these -- they are not 500s).
- **Truly unexpected exceptions** (anything else -- "a bug, unhandled
  type"): marked `failed` with the error recorded, and a generic
  "something went wrong, this has been logged" `AskResponse` is returned
  instead of raising -- section 4's literal requirement. `AskResponse` has
  no dedicated error variant (a real, minor schema gap, not invented here),
  so this reuses whichever `route_taken` the caller was attempting (an
  explicit apologetic message and `confidence=0.0`) -- the closest honest
  fit the existing shape allows.

`answer_question` and `triage_incident` share the exact same
record-execution / invoke-graph / handle-failure bookkeeping (they differ
only in which graph is built and how the initial `GraphState` is seeded),
factored into `_run_graph_and_record` rather than duplicated -- the same
DRY reasoning `core/incidents/repository.py`'s module docstring gives for
its own generic `**fields` updaters.

`generate_postmortem` does **not** go through `_run_graph_and_record` (it
isn't graph-based at all -- AGENT_WORKFLOWS.md section 2.5's "linear
pipeline, no routing logic," see `agents.postmortem.pipeline`'s module
docstring) and, more importantly, does **not** follow this module's
two-tier failure handling either: every failure there -- expected or
unexpected -- is marked `failed` and re-raised, never converted into a
fabricated "degraded" `Postmortem`. `AskResponse` has an honest
"something went wrong" shape (`answer` is just a string a human reads and
discards); `Postmortem` does not -- every field is meant to be real,
reviewable content that a human may click "approve" on. Fabricating one
to satisfy "never raise" would risk a human approving a postmortem that
silently says nothing useful, which is worse than the call simply raising.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import repository
from app.agents.graph import GraphState, build_graph, build_investigation_graph
from app.agents.llm import get_llm
from app.agents.postmortem.pipeline import run_postmortem_pipeline
from app.core.exceptions import EKIPError
from app.core.incidents import service as incidents_service
from app.core.incidents.schemas import ActionItem
from app.shared.config.logging import get_logger
from app.shared.schemas import AskResponse, Identity, TriggerSource

logger = get_logger(__name__)

_GENERIC_FAILURE_MESSAGE = (
    "Something went wrong while processing this request. This has been logged."
)


async def answer_question(
    session: AsyncSession,
    query: str,
    incident_id: uuid.UUID | None,
    actor: Identity,
    *,
    trigger_source: TriggerSource = "core_api",
) -> AskResponse:
    """Answer `query` end-to-end: Retrieval Agent -> Confidence Evaluation
    -> Answer Agent (when confidence is high enough) or the Investigation
    Agent (when it isn't) -- see `agents.graph.build_graph`'s docstring.

    `trigger_source` defaults to `"core_api"`: PROJECT_PLAN.md section 9's
    own preamble marks module "Public API" signatures as "conceptual, not
    literal code," and neither a real REST layer (`api/`, not yet built) nor
    MCP (Milestone 8) exists yet to supply a real value -- `"core_api"` is
    the only trigger source honestly true of any caller today. Whichever of
    those is built first should pass its own real value explicitly.
    """
    llm = get_llm()
    graph = build_graph(session, llm)
    initial_state = GraphState(query=query, incident_id=incident_id, actor=actor)

    return await _run_graph_and_record(
        session,
        agent_name="answer_question",
        trigger_source=trigger_source,
        input_summary={
            "query": query,
            "incident_id": str(incident_id) if incident_id is not None else None,
        },
        graph=graph,
        initial_state=initial_state,
        fallback_route="answer",
    )


async def triage_incident(
    session: AsyncSession,
    incident_id: uuid.UUID,
    actor: Identity,
    *,
    trigger_source: TriggerSource = "core_api",
) -> AskResponse:
    """Triage `incident_id`: enters directly at the Investigation Agent
    (API_DESIGN.md section 2 / AGENT_WORKFLOWS.md section 11.3's
    request-flow diagram), bypassing Retrieval Agent/Confidence Evaluation
    entirely -- triage always investigates, unlike `answer_question`'s
    confidence-routed path. Uses `build_investigation_graph`
    (`agents.graph`), a second, separate compiled graph containing only the
    Investigation Agent node -- see that function's own docstring for why.

    The query handed to the Investigation Agent is built from the
    incident's own `title`/`description` (there is no separate free-text
    question for a triage call the way there is for `answer_question`).

    Raises whatever `core.incidents.service.get_incident` raises (e.g.
    `NotFoundError` for a bad or cross-organization `incident_id`) -- an
    expected `EKIPError`, left to propagate per this module's own two-tier
    failure handling, not caught here. No `agent_executions` row is
    recorded for that case: no agent work was ever attempted, the same as
    a request that fails validation before reaching any business logic.
    """
    incident = await incidents_service.get_incident(
        session, actor, actor.organization_id, incident_id
    )
    query = f"{incident.title}\n\n{incident.description}"

    llm = get_llm()
    graph = build_investigation_graph(session, llm)
    initial_state = GraphState(query=query, incident_id=incident_id, actor=actor)

    return await _run_graph_and_record(
        session,
        agent_name="triage_incident",
        trigger_source=trigger_source,
        input_summary={"incident_id": str(incident_id)},
        graph=graph,
        initial_state=initial_state,
        fallback_route="investigation",
    )


async def generate_postmortem(
    session: AsyncSession,
    incident_id: uuid.UUID,
    actor: Identity,
    *,
    trigger_source: TriggerSource = "core_api",
) -> tuple[str, list[ActionItem]]:
    """Compute postmortem content for `incident_id` (API_DESIGN.md section 2 /
    AGENT_WORKFLOWS.md section 2.5): timeline reconstruction -> root-cause
    extraction -> action-item generation (`agents.postmortem.pipeline`).

    Returns `(root_cause, action_items)` -- **not** a persisted `Postmortem`
    row. Persisting into the `postmortems` table is deliberately not this
    function's job: `core.incidents.service.create_postmortem`'s own
    docstring already documents this exact split ("core/incidents calls
    `agents.generate_postmortem`, which returns computed content only;
    core/incidents then persists it, since agents never writes to this
    table directly"). Task #25's actual glue,
    `core.incidents.service.trigger_postmortem_generation`, is what calls
    this function and then calls `create_postmortem` with the result, under
    an internal `agent:postmortem_agent` identity
    (AGENT_WORKFLOWS.md section 2.5's required `generated_by`) rather than
    `actor` -- see that function's own docstring. An earlier draft of this
    function (task #24) called `create_postmortem` itself; that silently
    contradicted `create_postmortem`'s own pre-existing documented design
    and was corrected here rather than left in place.

    `actor` is used only to read the incident's timeline under the normal
    tenant-isolation rules -- whatever identity is ultimately authorized to
    trigger generation (checked by `trigger_postmortem_generation`, not
    here).

    See module docstring for why this does not follow `answer_question`/
    `triage_incident`'s two-tier failure handling: every failure here is
    marked `failed` and re-raised, never converted into fabricated content.
    """
    execution = await repository.insert_agent_execution(
        session,
        organization_id=actor.organization_id,
        agent_name="generate_postmortem",
        trigger_source=trigger_source,
        input_summary={"incident_id": str(incident_id)},
    )

    try:
        timeline_entries = await incidents_service.get_timeline(
            session, actor, actor.organization_id, incident_id
        )

        llm = get_llm()
        root_cause, action_items = await run_postmortem_pipeline(llm, timeline_entries)
    except Exception as exc:
        await repository.update_agent_execution(
            session,
            execution.id,
            status="failed",
            error_detail=str(exc)[:2000],
            completed_at=datetime.now(timezone.utc),
        )
        raise

    await repository.update_agent_execution(
        session,
        execution.id,
        status="succeeded",
        completed_at=datetime.now(timezone.utc),
    )
    return root_cause, action_items


async def _run_graph_and_record(
    session: AsyncSession,
    *,
    agent_name: str,
    trigger_source: TriggerSource,
    input_summary: dict[str, str | None],
    graph: Any,
    initial_state: GraphState,
    fallback_route: Literal["answer", "investigation"],
) -> AskResponse:
    """Shared bookkeeping behind both `answer_question` and
    `triage_incident`: record one `agent_executions` row, invoke `graph`
    against `initial_state`, and apply this module's own two-tier failure
    handling (see module docstring). `fallback_route` picks which
    `AskResponse.route_taken` the generic-failure response uses -- the
    closest honest label for whichever entry point was actually being
    attempted, since `AskResponse` has no dedicated error variant.
    """
    execution = await repository.insert_agent_execution(
        session,
        organization_id=initial_state.actor.organization_id,
        agent_name=agent_name,
        trigger_source=trigger_source,
        input_summary=input_summary,
    )

    try:
        raw_final_state = await graph.ainvoke(initial_state)
    except EKIPError as exc:
        await repository.update_agent_execution(
            session,
            execution.id,
            status="failed",
            error_detail=str(exc)[:2000],
            completed_at=datetime.now(timezone.utc),
        )
        raise
    except Exception as exc:
        logger.error(
            "agent_execution_unexpected_failure",
            agent_name=agent_name,
            query=initial_state.query,
            actor=initial_state.actor.audit_tag,
            error=str(exc),
        )
        await repository.update_agent_execution(
            session,
            execution.id,
            status="failed",
            error_detail=str(exc)[:2000],
            completed_at=datetime.now(timezone.utc),
        )
        return AskResponse(
            confidence=0.0,
            route_taken=fallback_route,
            answer=_GENERIC_FAILURE_MESSAGE,
            citations=[],
        )

    final_state = (
        raw_final_state
        if isinstance(raw_final_state, GraphState)
        else GraphState.model_validate(raw_final_state)
    )

    if final_state.result is None:
        # Every real path through either graph always sets `result` --
        # reaching here means a node returned without setting it: a real bug
        # in that graph's own wiring, not a documented degradation case, so
        # it gets the same unexpected-failure treatment as the `except
        # Exception` branch above.
        logger.error("agent_execution_graph_produced_no_result", agent_name=agent_name)
        await repository.update_agent_execution(
            session,
            execution.id,
            status="failed",
            error_detail="graph completed with no result",
            completed_at=datetime.now(timezone.utc),
        )
        return AskResponse(
            confidence=0.0,
            route_taken=fallback_route,
            answer=_GENERIC_FAILURE_MESSAGE,
            citations=[],
        )

    await repository.update_agent_execution(
        session,
        execution.id,
        status="succeeded",
        confidence_score=final_state.confidence_score,
        completed_at=datetime.now(timezone.utc),
    )
    return final_state.result
