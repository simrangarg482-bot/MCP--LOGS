"""The single LangGraph state schema threaded through every node, plus the
actual `StateGraph` node wiring and compiled graph -- per the file layout
PROJECT_PLAN.md section 10 lays out: "graph.py -- state schema + node wiring
(the composing layer)".

Owned by: agents/. `GraphState` is defined here, not in `agents/schemas.py`
or `shared/schemas/`, because it is purely an internal wiring detail of this
one graph -- no other module ever sees a `GraphState` instance; every
caller-facing shape it eventually produces (`AskResponse`) is a plain,
already-shared type.

Per AGENT_WORKFLOWS.md section 1, this is one typed object carrying the
query, retrieved evidence, confidence score, and resolved `Identity` through
every node -- never a raw dict, so every node's inputs/outputs are checked
by the type system rather than by convention.

`evidence`/`hypotheses` are populated by the Investigation Agent
(`agents.investigation.node`, Milestone 7, task #23) -- this is the one
shared state object for the whole graph (investigation included), not a
Milestone-6-only subset that would need a breaking change to extend later.

**Graph wiring (task #21, real Investigation Agent wired in task #23):**
`build_graph(session, llm)` composes Retrieval Agent -> Confidence
Evaluation -> a conditional edge on `state.route` -> Answer Agent (when
`route == "answer"`) or the Investigation Agent (when
`route == "investigation"`, `agents.investigation.node`). Rebuilt and
recompiled on every call rather than once at import time: `session` is
request-scoped (the same reasoning `agents.retrieval.node`'s module
docstring gives for its own factory pattern), so nodes closing over it
cannot be shared across requests; graph compilation itself has no
meaningful cost that would make caching worth the complexity.

**`build_investigation_graph(session, llm)`** (task #23) is a second,
separate compiled graph containing only the Investigation Agent node --
built for `agents.service.triage_incident`, which enters directly at the
Investigation Agent per AGENT_WORKFLOWS.md section 11.3's request-flow
diagram, bypassing Retrieval Agent/Confidence Evaluation entirely (triage
always investigates, unlike `answer_question`'s confidence-routed path).
A separate graph, not `build_graph` re-entered mid-way: forcing
`build_graph`'s conditional edge down the investigation branch would
require faking a `confidence_score`/`route` on the initial state for a
stage that never actually ran, which is worse than just not running that
stage's edges at all.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.memory.schemas import RecalledMemory
from app.retrieval.schemas import ScoredChunk
from app.shared.schemas import AskResponse, EvidenceItem, Identity, RootCauseHypothesis


class GraphState(BaseModel):
    """State threaded through every node of the `answer_question` /
    `triage_incident` graph (AGENT_WORKFLOWS.md section 1).

    Not frozen: unlike the value objects it carries (`Identity`,
    `ScoredChunk`, ...), this is a mutable working document nodes
    incrementally populate as the graph advances -- each node returns the
    fields it updates, which LangGraph merges into the state passed to the
    next node.
    """

    # --- input ---------------------------------------------------------
    query: str
    incident_id: uuid.UUID | None = None
    actor: Identity

    # --- retrieval stage -------------------------------------------------
    retrieved_chunks: list[ScoredChunk] = Field(default_factory=list)
    rewritten_query: str | None = None

    # --- persistent memory (Priority 4, app.core.memory) -------------------
    # Deliberately a SEPARATE field from `retrieved_chunks`, not a few more
    # `ScoredChunk`s appended to it. Two reasons, both load-bearing:
    #   1. `ScoredChunk`s become numbered, citable sources
    #      (`agents.answer.generation.build_context_block` ->
    #      `build_citations`). Memory is context, not evidence, and must
    #      never be presented as a citation -- an answer's factual claims
    #      stay grounded in retrieved documents.
    #   2. Memory has no `document_id`/offsets, so forging one as a
    #      `ScoredChunk` would mean inventing provenance it does not have.
    # Populated once, before the graph runs (`agents.service.answer_question`),
    # rather than by a node: it needs no LLM and no retry, so a node would add
    # graph surface for nothing. Empty list = no relevant memory, in which
    # case every downstream prompt is byte-identical to pre-memory behavior.
    recalled_memories: list[RecalledMemory] = Field(default_factory=list)

    # --- confidence stage --------------------------------------------------
    confidence_score: float | None = None
    # Kept for observability, not just the final number -- "why did this get
    # routed to investigation?" must be answerable from stored state, per
    # this field's rationale in AGENT_WORKFLOWS.md section 1.
    confidence_signals: dict[str, float] = Field(default_factory=dict)

    # --- routing ------------------------------------------------------------
    route: Literal["answer", "investigation"] | None = None

    # --- investigation stage (Milestone 7 -- see module docstring) ----------
    evidence: list[EvidenceItem] = Field(default_factory=list)
    hypotheses: list[RootCauseHypothesis] = Field(default_factory=list)

    # --- output --------------------------------------------------------
    result: AskResponse | None = None

    # --- control -------------------------------------------------------
    # Per-node retry tracking (AGENT_WORKFLOWS.md section 4: up to 2 retries
    # per node with exponential backoff before converting to that node's
    # terminal-condition behavior).
    retry_count: dict[str, int] = Field(default_factory=dict)
    terminal_error: str | None = None


def _route_after_confidence(state: GraphState) -> str:
    """Conditional-edge selector run after `confidence_evaluation_node`."""
    return "answer" if state.route == "answer" else "investigation"


def build_graph(session: AsyncSession, llm: BaseChatModel) -> Any:
    """Compose and compile the graph described in this module's docstring,
    bound to `session`/`llm` for the lifetime of one invocation. Returns a
    LangGraph `CompiledStateGraph` (left untyped here -- LangGraph does not
    export a stable public type name for it across the pinned version range).

    The node-building imports below are deliberately local to this function,
    not module-level: `agents.answer.node`, `agents.confidence`,
    `agents.retrieval.node`, and `agents.investigation.node` each import
    `GraphState` *from this module* for their own type hints, which would
    otherwise be a circular import at module-load time (this module trying
    to import them, before `GraphState` even finishes being defined, while
    they simultaneously try to import `GraphState` back from this
    not-yet-fully-loaded module). Deferring these imports to call time --
    well after `GraphState` is fully defined -- is the standard, safe way to
    break that cycle without moving `GraphState` out of this file (which the
    file layout in this module's own docstring, and PROJECT_PLAN.md section
    10, both call for keeping here).
    """
    from app.agents.answer.node import make_answer_agent_node
    from app.agents.confidence import confidence_evaluation_node
    from app.agents.investigation.node import make_investigation_agent_node
    from app.agents.retrieval.node import make_retrieval_agent_node

    graph = StateGraph(GraphState)
    graph.add_node("retrieval_agent", make_retrieval_agent_node(session, llm))
    graph.add_node("confidence_evaluation", confidence_evaluation_node)
    graph.add_node("answer_agent", make_answer_agent_node(llm))
    graph.add_node("investigation_agent", make_investigation_agent_node(session, llm))

    graph.set_entry_point("retrieval_agent")
    graph.add_edge("retrieval_agent", "confidence_evaluation")
    graph.add_conditional_edges(
        "confidence_evaluation",
        _route_after_confidence,
        {"answer": "answer_agent", "investigation": "investigation_agent"},
    )
    graph.add_edge("answer_agent", END)
    graph.add_edge("investigation_agent", END)

    return graph.compile()


def build_investigation_graph(session: AsyncSession, llm: BaseChatModel) -> Any:
    """Compile a second, separate graph containing only the Investigation
    Agent node -- see this module's docstring for why `triage_incident`
    needs its own graph rather than re-entering `build_graph`'s conditional
    edge.

    The Investigation Agent node import is local for the same circular-
    import reason `build_graph` defers its own node imports -- see that
    function's docstring.
    """
    from app.agents.investigation.node import make_investigation_agent_node

    graph = StateGraph(GraphState)
    graph.add_node("investigation_agent", make_investigation_agent_node(session, llm))
    graph.set_entry_point("investigation_agent")
    graph.add_edge("investigation_agent", END)

    return graph.compile()
