"""Pydantic contracts for the output of `agents.answer_question` /
`agents.triage_incident` (API_DESIGN.md section "Ask / Question answering"),
shared across module boundaries.

Owned by: shared/ (PROJECT_PLAN.md section 9.11: "common Pydantic schemas
(Citation, etc.) used by more than one module"). These types are genuinely
cross-module -- produced by `agents/`, consumed by `api/` (REST responses,
not yet built), `mcp/` (tool responses, Milestone 8), and `core/incidents`
(persisting `TriageResult`-shaped data into `incident_timeline`) -- so they
live here rather than in `agents/schemas.py`, the same reasoning that already
places `Identity` in shared/ rather than in whichever module resolves it
first.

`InvestigationResult`/`EvidenceItem`/`RootCauseHypothesis` are defined now
(Milestone 6) even though the Investigation Agent that populates them is
Milestone 7 work (PROJECT_PLAN.md section 6.4) -- `AskResponse.investigation`
needs a concrete type to declare today, and `GraphState` (agents/graph.py)
carries `evidence`/`hypotheses` fields per AGENT_WORKFLOWS.md's shared-state
design regardless of which milestone actually populates them. Until
Milestone 7 lands, the `route == "investigation"` path is unreachable, but
the shape is real, not a placeholder to be redefined later.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Citation(BaseModel):
    """One grounded reference backing a sentence in `AskResponse.answer` --
    API_DESIGN.md's contract for making the Answer Agent's grounding
    verifiable rather than asserted (PROJECT_PLAN.md section 5.7).
    """

    model_config = ConfigDict(frozen=True)

    document_id: uuid.UUID
    chunk_id: uuid.UUID
    source_url: str | None
    excerpt: str


class EvidenceItem(BaseModel):
    """One piece of *verified, retrieved* evidence gathered by the
    Investigation Agent's sub-stage A (PROJECT_PLAN.md section 6.4) --
    explicitly not AI-generated, the structural half of the "verified vs.
    AI-generated" distinction API_DESIGN.md requires.

    `"issue"` was added to `source` alongside the GitHub connector's
    extension to issues/PRs/commits (`"pull_request"`/`"commit"` already
    existed here, built in anticipation of exactly this before the connector
    itself could produce either). `"monitoring"` was added alongside
    `agents.investigation.live.MonitoringLiveSource`'s registration into
    `_LIVE_SOURCES` -- that class still always returns an empty evidence
    list (no real PagerDuty/Datadog/Grafana/etc. integration exists yet),
    but now has a real, typed `source` value to use whenever one is built,
    rather than the previous state where no legitimate value existed for it
    at all. `source_timestamp`/`metadata` are
    additive fields (default `None`/`{}`, so every pre-existing construction
    site -- postmortem evidence, the zero-evidence/empty-evidence paths --
    keeps working unchanged): `source_timestamp` is the *original* GitHub
    object's own date (a commit's authored date, a PR/issue's `created_at`)
    -- not `retrieved_at`, which is when this evidence-gathering step ran,
    not when the underlying event happened. `metadata` carries whatever
    kind-specific facts the connector attached to the document
    (`author`/`labels`/`changed_files`/`reviews`/... -- see
    `app.ingestion.connectors.github`'s module docstring for the exact key
    set per kind) verbatim, structured, rather than baked into `summary`'s
    prose -- this type is also persisted into `incident_timeline`
    (`core.incidents.service.record_investigation_result`) and may be read
    by a future UI/MCP resource, so keeping these facts as real fields
    (not something a reader would need to regex back out of a sentence)
    matches the structured-schema convention used everywhere else in this
    file.
    """

    model_config = ConfigDict(frozen=True)

    source: Literal[
        "github",
        "pull_request",
        "commit",
        "issue",
        "slack",
        "jira",
        "deployment",
        "postmortem",
        "monitoring",
    ]
    reference: str  # PR number, message link, ticket ID, etc.
    summary: str
    retrieved_at: datetime
    source_timestamp: datetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class RootCauseHypothesis(BaseModel):
    """One AI-generated hypothesis from the Investigation Agent's sub-stage
    B (PROJECT_PLAN.md section 6.4). `supporting_evidence_ids` must
    reference real `EvidenceItem.reference` values from the same
    investigation -- a hypothesis with none is rejected by a validation
    step and never surfaced (AGENT_WORKFLOWS.md section 2.4), never
    constructed here without at least one entry.
    """

    model_config = ConfigDict(frozen=True)

    description: str
    confidence: float
    supporting_evidence_ids: list[str]


class InvestigationResult(BaseModel):
    """Populated on `AskResponse.investigation` only when
    `route_taken == "investigation"`. Keeps `evidence` (verified) and
    `hypotheses` (generated) as separate lists rather than one merged
    collection -- exactly the distinction PROJECT_PLAN.md section 5.5
    requires stay structural, not a prompt convention.

    `review_status`/`critique_verdict`/`revision_count`/`critique_issues`
    are Priority 7 additions (`agents.investigation.critique`) -- all
    default so every pre-existing construction site keeps working
    unchanged. `review_status` is the one field a consumer MUST check
    before trusting `hypotheses` as "reviewed": `"reviewed"` means the
    bounded critique pass actually completed (whatever its verdict);
    `"not_reviewed"` means critique never ran (disabled, or nothing to
    critique); `"review_failed"` means critique was attempted but could not
    complete (model/timeout/malformed-output failure) -- `hypotheses` in
    that case is the best available (pre-critique, or pre-revision) result,
    never silently presented as if it had passed review. `critique_issues`
    holds short, structured category tags only (e.g.
    `"overconfidence:hypothesis_0"`) -- never raw model reasoning or a
    critique transcript (see that module's own docstring).
    """

    model_config = ConfigDict(frozen=True)

    evidence: list[EvidenceItem]
    hypotheses: list[RootCauseHypothesis]
    suggested_owner_team: str | None
    suggested_next_steps: list[str]
    review_status: Literal["not_reviewed", "reviewed", "review_failed"] = "not_reviewed"
    critique_verdict: Literal["accept", "revise", "reject"] | None = None
    revision_count: int = 0
    critique_issues: list[str] = Field(default_factory=list)


class AskResponse(BaseModel):
    """The return shape of `agents.answer_question` / `agents.triage_incident`
    (API_DESIGN.md section 2). Exactly one of `answer`/`investigation` is
    populated, matching `route_taken`.

    `answer_mode` (Priority 10) is the machine-readable, authoritative
    semantic outcome of the answer path -- `"answered"` (a substantive,
    grounded answer was produced) or `"no_answer"` (the system intentionally
    declined because the evidence was insufficient or the draft could not be
    grounded), set by `agents.answer.node` (the single authority for this
    decision; see that module's `generate_answer_with_outcome`). Only
    two values, not three: this codebase's production Answer Agent has no
    "qualified/hedged answer" generation mode today (`agents.answer.
    sufficiency.SufficiencyVerdict`'s `"partial"` is currently treated
    identically to `"insufficient"` -- both decline), so a `"qualified_
    answer"` value here would misrepresent capability the product doesn't
    actually have. Deliberately `None`, not defaulted to `"answered"`, for
    every case where no answer-path decision was actually made: the
    `route_taken == "investigation"` path, the two generic-failure
    fallbacks in `agents.service._run_graph_and_record` (an infrastructure
    failure is not a semantic refusal -- conflating the two would let a bug
    masquerade as an epistemically-correct decline), and any historical
    response recorded before this field existed. `None` here means
    "unknown/not applicable," never a false claim either way.
    """

    model_config = ConfigDict(frozen=True)

    confidence: float
    route_taken: Literal["answer", "investigation"]
    answer: str | None = None
    answer_mode: Literal["answered", "no_answer"] | None = None
    citations: list[Citation] = Field(default_factory=list)
    investigation: InvestigationResult | None = None


class GapReport(BaseModel):
    """One recommendation produced by the Knowledge Gap Agent (Milestone 9,
    AGENT_WORKFLOWS.md section 2.6 / PROJECT_PLAN.md section 6.6 /
    API_DESIGN.md section 2's `detect_knowledge_gaps() -> list[GapReport]`).

    Mirrors `app.database.models.agent_models.KnowledgeGapReport`'s columns
    (see that model's own docstring for why `status` is an addition beyond
    the original spec). `supporting_execution_ids` references
    `AgentExecution.id` values, not a persisted evidence-item shape --
    fetching the underlying executions is a separate lookup a reviewer's
    tooling can do if it wants the actual query text back.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    suggested_topic: str
    supporting_execution_ids: list[uuid.UUID]
    suggested_action: Literal["new_runbook", "update_existing"]
    related_document_id: uuid.UUID | None
    status: Literal["open", "dismissed"]
    created_at: datetime
    updated_at: datetime
