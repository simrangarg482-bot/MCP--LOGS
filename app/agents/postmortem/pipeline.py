"""Postmortem Agent's internal pipeline (PROJECT_PLAN.md section 6.5 /
AGENT_WORKFLOWS.md section 2.5): timeline reconstruction -> root-cause
extraction -> action-item generation. Structured report assembly --
actually creating the `Postmortem` row -- is `agents.service.
generate_postmortem`'s job, not this module's: persisting into a
`core/incidents`-owned table belongs directly next to that function's other
`core.incidents.service` calls, the same way `agents.service.triage_incident`
calls `core.incidents.service.get_incident` itself rather than through an
extra indirection layer here.

No `node.py`/`StateGraph` wiring in this subpackage, unlike
`agents/investigation/`: AGENT_WORKFLOWS.md section 2.5 is explicit that
"this is a linear pipeline, not a confidence-gated one" with "no routing
logic" -- there is no conditional edge for LangGraph's machinery to add
value over, and `generate_postmortem` is never reached via the
`answer_question`/`triage_incident` graph's `GraphState` at all (it runs
after a human marks an incident resolved, an entirely separate trigger).
A plain async function pipeline is the same choice `ingestion.service.
run_ingestion_job` already makes for its own linear, non-branching flow.
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from app.agents.postmortem.action_items import generate_action_items
from app.agents.postmortem.root_cause import extract_root_cause
from app.agents.postmortem.timeline import build_narrative, latest_investigation_hypotheses
from app.core.incidents.schemas import ActionItem, TimelineEntry


async def run_postmortem_pipeline(
    llm: BaseChatModel, timeline_entries: list[TimelineEntry]
) -> tuple[str, list[ActionItem]]:
    """Run steps 1-3 of the Postmortem Agent over `timeline_entries`
    (`core.incidents.service.get_timeline`'s output), returning
    `(root_cause, action_items)` for the caller to assemble into a
    `Postmortem`.
    """
    narrative = build_narrative(timeline_entries)
    candidate_hypotheses = latest_investigation_hypotheses(timeline_entries)

    root_cause = await extract_root_cause(llm, narrative, candidate_hypotheses)
    action_items = await generate_action_items(llm, narrative, root_cause)

    return root_cause, action_items
