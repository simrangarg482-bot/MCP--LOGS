"""Knowledge Gap Agent (Milestone 9, AGENT_WORKFLOWS.md section 2.6 /
PROJECT_PLAN.md section 6.6).

Owned by: agents/knowledge_gap. Not part of the per-question
`answer_question`/`triage_incident` graph (`agents/graph.py`) -- this runs as
a separate, scheduled process (`app/agents/workers/`), over one
organization's recent `agent_executions` at a time.

A linear pipeline (`pipeline.py`), not a `StateGraph`: the same reasoning
`agents.postmortem.pipeline`'s own module docstring gives for the Postmortem
Agent applies identically here -- fetch low-confidence executions -> cluster
-> synthesize a topic -> resolve a suggested action -> persist always runs
in the same order with no routing decision anywhere in it, so LangGraph's
state-machine machinery would add ceremony without buying anything.

`clustering.py` resolves AGENT_WORKFLOWS.md's previously-open item
("clustering method/threshold ... k-means vs a simpler similarity-threshold
grouping -- not yet decided") in favor of similarity-threshold (leader)
clustering -- see that module's own docstring for the full reasoning.

**Never auto-creates a `documents` row** (AGENT_WORKFLOWS.md: "this keeps
the agent's blast radius limited to 'suggest'"): a `GapReport` is a
recommendation surfaced via `GET /knowledge/gaps`; turning one into an
actual proposed runbook is a separate, explicit human action via
`core.knowledge.service.propose_document` (the MCP `propose_runbook_update`
tool), never triggered automatically from here.
"""

from __future__ import annotations
