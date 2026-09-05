"""Public interface for agents/ (PROJECT_PLAN.md section 9.7):
`answer_question`, `triage_incident`, `generate_postmortem`, and, as of
Milestone 9, `detect_knowledge_gaps` / `list_gap_reports` -- the actual
`agents/knowledge_gap/pipeline.py` logic lives in its own subpackage (same
split as `agents.postmortem.pipeline`); this module's job is the same thin
wrapper role it plays for every other agent: resolve settings/LLM, call the
pipeline, and (for `detect_knowledge_gaps`) record the run's own
`agent_executions` row.

`detect_knowledge_gaps` is called per-organization on a schedule (`app/
agents/workers/`, since `app.ingestion.workers` may not import `app.agents`
-- see that package's own module docstring), not from any per-question
graph -- AGENT_WORKFLOWS.md section 2.6 is explicit this agent is "not part
of the per-question flow."

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

`search_similar_incidents`/`search_recent_changes` (added for Milestone 8's
MCP tool handlers, API_DESIGN.md section 3) are thin `retrieval.search`
passthroughs that resolve `SearchFilters` from `actor` before calling in --
exactly the boundary `retrieval/schemas.py`'s own module docstring assigns
to a caller ("`SearchFilters` is a plain value object ... whatever calls
`retrieval.search()` ... is responsible for resolving from an `Identity`").
They live here, not in `app/mcp/tools/` directly, because `app.mcp` may not
import `app.retrieval` at all (pyproject.toml's "mcp never touches database
or retrieval directly" contract forbids that *direct* import outright, not
just the transitive chains `allow_indirect_imports` relaxes) -- `agents/`
has no such restriction, so it is the correct place for MCP's search tools
to route through, the same way `app.mcp.auth`/`app.mcp.dispatch` route
through `core/` rather than reaching into `app.database` themselves.

`generate_postmortem` does **not** go through `_run_graph_and_record` (it
isn't graph-based at all -- AGENT_WORKFLOWS.md section 2.5's "linear
pipeline, no routing logic," see `agents.postmortem.pipeline`'s module
docstring) and does **not** follow the "return a fabricated degraded
result instead of raising" half of this module's failure handling: every
failure there is marked `failed` and re-raised, never converted into a
fabricated "degraded" `Postmortem`. `AskResponse` has an honest
"something went wrong" shape (`answer` is just a string a human reads and
discards); `Postmortem` does not -- every field is meant to be real,
reviewable content that a human may click "approve" on. Fabricating one
to satisfy "never raise" would risk a human approving a postmortem that
silently says nothing useful, which is worse than the call simply raising.

It *does* still classify what it re-raises, like the rest of this module:
the pipeline's LLM steps run under `agents.retry.call_with_retry`, and a
transient provider failure that outlasts those retries
(`agents.llm.TRANSIENT_LLM_ERRORS`) is re-raised as `ServiceUnavailableError`
(503, "retry later"), not left to become a generic 500. Everything else --
a bad `incident_id`, a real bug -- propagates unchanged. Before this,
`generate_postmortem` was the one agent entry point where an LLM outage
surfaced to the caller as an unhandled crash rather than a typed,
retryable error.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from langchain_core.callbacks import UsageMetadataCallbackHandler
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import cost_budget, repository
from app.agents.graph import GraphState, build_graph, build_investigation_graph
from app.agents.knowledge_gap import repository as knowledge_gap_repository
from app.agents.knowledge_gap.pipeline import detect_knowledge_gaps as _run_knowledge_gap_pipeline
from app.agents.llm import TRANSIENT_LLM_ERRORS, get_llm
from app.agents.postmortem.pipeline import run_postmortem_pipeline
from app.agents.schemas import AgentExecution, AgentExecutionStats
from app.agents.telemetry import get_estimated_cost_usd, summarize_usage
from app.core.exceptions import EKIPError, PermissionDeniedError, ServiceUnavailableError
from app.core.incidents import service as incidents_service
from app.core.incidents.schemas import ActionItem
from app.core.memory import service as memory_service
from app.core.users.service import require_permission
from app.database.models.agent_models import KnowledgeGapReport
from app.retrieval import service as retrieval_service
from app.retrieval.ranking.fusion import reciprocal_rank_fusion
from app.retrieval.schemas import CollectionName, ScoredChunk, SearchFilters
from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings
from app.shared.schemas import AskResponse, GapReport, Identity, TriggerSource

logger = get_logger(__name__)

_GENERIC_FAILURE_MESSAGE = (
    "Something went wrong while processing this request. This has been logged."
)

# Same permission `core.knowledge.service` requires for its proposed-
# documents review queue -- see `list_gap_reports`'s docstring.
_GAP_REVIEW_PERMISSION = "knowledge:review"

# Same permission code `core.observability.service.get_mcp_dashboard`
# requires -- see `get_agent_execution_stats`'s docstring on why this is a
# duplicated constant, not a shared import.
_OBSERVABILITY_READ_PERMISSION = "observability:read"

# Metadata keys checked (in order) for a chunk's own recency timestamp by
# `search_recent_changes`'s best-effort `since` filter -- see that
# function's docstring for why this is inherently a best-effort, not a hard
# guarantee.
_RECENCY_METADATA_KEYS = ("source_timestamp", "updated_at", "timestamp")

# Collections `search_recent_changes` searches by default (`collection=None`)
# -- see that function's docstring: GitHub commit/PR/issue history lands in
# "documentation" (no file extension), while actual changed source files land
# in "code" -- "recent changes" means both, not either alone. Excludes
# "conversations" deliberately: chat mentions of a change are not the change
# itself, and a caller that wants that too can still pass an explicit
# `collection` for one collection at a time (unchanged, pre-existing behavior).
_RECENT_CHANGES_COLLECTIONS: tuple[CollectionName, ...] = ("documentation", "code")


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

    # Persistent memory (Priority 4). Recalled once, here, before the graph
    # runs -- not inside a node: it needs no LLM and has no retryable failure
    # mode, so a node would add graph surface for nothing. Authorization
    # happens inside `recall_relevant`'s SQL (scoped to this actor's
    # organization, own user-private memory, and projects they hold a
    # membership in), so nothing unauthorized can reach the state object.
    #
    # A failure here must never cost the user their answer: memory is
    # supplementary context, and an answer grounded in retrieved evidence is
    # still a correct answer without it. So this degrades to "no memory"
    # rather than propagating -- the same "degrade, don't fail" treatment
    # `agents.retrieval.node` already gives an exhausted search.
    try:
        recalled = await memory_service.recall_relevant(session, actor, query)
    except Exception as exc:  # noqa: BLE001 -- memory is never worth failing a question over
        logger.warning(
            "memory_recall_failed_degrading",
            actor=actor.audit_tag,
            organization_id=str(actor.organization_id),
            error=str(exc),
        )
        recalled = []

    initial_state = GraphState(
        query=query,
        incident_id=incident_id,
        actor=actor,
        recalled_memories=recalled,
    )

    return await _run_graph_and_record(
        session,
        agent_name="answer_question",
        trigger_source=trigger_source,
        input_summary={
            "query": query,
            "incident_id": str(incident_id) if incident_id is not None else None,
            # Count only, never the memory text: `input_summary` is read by
            # `GET /observability/agents` (an org-level `observability:read`
            # surface), and a user-private memory's content must not surface
            # there. The count is what makes "did memory influence this
            # answer?" answerable without exposing what was remembered.
            "recalled_memory_count": len(recalled),
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
    await cost_budget.check_cost_budget(session, actor.organization_id)

    execution = await repository.insert_agent_execution(
        session,
        organization_id=actor.organization_id,
        agent_name="generate_postmortem",
        trigger_source=trigger_source,
        input_summary={"incident_id": str(incident_id)},
    )

    # Phase 5.4/5.7: `with_config` binds the handler to every future
    # `.ainvoke` call this specific `llm` instance makes, without
    # `run_postmortem_pipeline`/`extract_root_cause`/`generate_action_items`
    # needing to know or thread a callbacks parameter through at all -- see
    # `app.agents.service._run_graph_and_record`'s own comment for the
    # graph-based equivalent of this same pattern.
    usage_handler = UsageMetadataCallbackHandler()

    try:
        timeline_entries = await incidents_service.get_timeline(
            session, actor, actor.organization_id, incident_id
        )

        llm = get_llm().with_config(callbacks=[usage_handler])
        root_cause, action_items = await run_postmortem_pipeline(llm, timeline_entries)
    except TRANSIENT_LLM_ERRORS as exc:
        # A provider outage that survived `run_postmortem_pipeline`'s
        # retries. Still "mark failed and re-raise" (this module never
        # fabricates a degraded `Postmortem` -- see module docstring), but
        # re-raised as a 503 "retry later", not the generic 500 a bare
        # re-raise would produce: the request is not broken, the upstream is
        # briefly unavailable. Mirrors the graph-based agents, whose nodes
        # already treat an exhausted LLM retry as a degradation rather than
        # a crash -- `generate_postmortem` was previously the one agent
        # entry point where this surfaced as an unhandled 500.
        await repository.update_agent_execution(
            session,
            execution.id,
            status="failed",
            error_detail=str(exc)[:2000],
            completed_at=datetime.now(UTC),
            **summarize_usage(usage_handler),
        )
        raise ServiceUnavailableError(
            "The language model provider is temporarily unavailable; postmortem "
            "generation could not complete. Retry shortly.",
            error_code="agents.llm_unavailable",
        ) from exc
    except Exception as exc:
        await repository.update_agent_execution(
            session,
            execution.id,
            status="failed",
            error_detail=str(exc)[:2000],
            completed_at=datetime.now(UTC),
            **summarize_usage(usage_handler),
        )
        raise

    await repository.update_agent_execution(
        session,
        execution.id,
        status="succeeded",
        completed_at=datetime.now(UTC),
        **summarize_usage(usage_handler),
    )
    return root_cause, action_items


async def search_similar_incidents(
    session: AsyncSession,
    description: str,
    actor: Identity,
    *,
    top_k: int = 10,
) -> list[ScoredChunk]:
    """Search for evidence resembling `description` (API_DESIGN.md section 3's
    `search_similar_incidents` MCP tool: `{description: str}` ->
    `list[ScoredChunk]`).

    API_DESIGN.md's table describes this as searching a `collection=
    "incidents"` -- no such collection exists (`retrieval.schemas.
    CollectionName` is `Literal["documentation", "code", "conversations"]`;
    that Literal's own comment already flags "nothing produces embeddable
    chunks for [incidents] today"). Passing a nonexistent collection would be
    a hard runtime type error, not a graceful degradation, so this searches
    every collection (`collection=None`, `retrieval.search`'s own
    all-collections default) instead -- a real, flagged gap versus the
    documented contract's literal wording, not a silent workaround.
    """
    filters = SearchFilters(organization_id=actor.organization_id)
    return await retrieval_service.search(session, description, filters, top_k)


async def search_recent_changes(
    session: AsyncSession,
    query: str,
    actor: Identity,
    *,
    since: datetime | None = None,
    top_k: int = 10,
    collection: CollectionName | None = None,
    repository: str | None = None,
) -> list[ScoredChunk]:
    """Search for recent code/documentation changes matching `query`
    (API_DESIGN.md section 3's `search_recent_changes` MCP tool:
    `{query: str, since?: str}` -> `list[ScoredChunk]`).

    `since`-based recency filtering is a best-effort, not a guarantee:
    `retrieval.schemas.SearchFilters` has no recency field (this project's
    "Open items" in API_DESIGN.md already flags "whether `search_recent_
    changes` needs its own dedicated retrieval collection or can filter the
    existing `code` collection by metadata recency" as an undecided
    question), so filtering happens client-side against each
    `ScoredChunk.metadata` entry (populated via `include_metadata=True`) --
    checked against whichever of `_RECENCY_METADATA_KEYS` is present. A
    chunk whose metadata carries none of those keys is kept rather than
    dropped, since "no timestamp available" is not the same claim as "not
    recent."

    `repository`, if given, restricts results to one GitHub repo (e.g.
    `"owner/name"`) via `SearchFilters.repository` -- valid for both
    collections `collection=None` (the default) searches, `"documentation"`
    and `"code"`; `PgVectorStore` raises for `"conversations"`, which
    carries no `repo_full_name` to filter on (so an explicit
    `collection="conversations"` call must not also pass `repository`).

    `collection=None` (the default) searches `_RECENT_CHANGES_COLLECTIONS`
    -- `"documentation"` *and* `"code"` -- and fuses the two ranked lists
    with the same `reciprocal_rank_fusion` `retrieval.service.search` uses
    internally to fuse dense+lexical results for one collection, applied
    here across collections instead. Fixed from an earlier version that
    searched exactly one collection: `app.ingestion.processors.chunking.
    classify_content_type` classifies by content *shape*, not by which
    connector produced it, so GitHub commit messages, PR bodies, and issue
    bodies (no file extension) land in `"documentation"`, the same as
    READMEs and other docs, while a repo's actual changed source files
    (real file extensions) land in `"code"`. "Recent changes" means both --
    a commit's own message/description *and* the file it touched -- so
    searching only one of the two silently hid the other's evidence, e.g.
    a real commit/PR/issue history match no better than an empty result
    just because a matching source-file chunk didn't happen to rank inside
    `top_k` in a single-collection search over just `"code"`, or vice
    versa. `"conversations"` (chat) is deliberately excluded from this
    default -- a Slack message mentioning a change is not the change
    itself -- but is still reachable via an explicit `collection=
    "conversations"` call, same as before.

    An explicit `collection` (any one of the three) restricts the search to
    exactly that collection, same single-collection behavior as before this
    fix -- e.g. the Investigation Agent's evidence-gathering sub-stage
    (PROJECT_PLAN.md section 6.4), which needs `"code"` and
    `"conversations"` as two separate, source-labeled steps, keeps working
    unchanged.
    """
    filters = SearchFilters(organization_id=actor.organization_id, repository=repository)
    if collection is None:
        result_lists = [
            await retrieval_service.search(
                session, query, filters, top_k, collection_name, include_metadata=True
            )
            for collection_name in _RECENT_CHANGES_COLLECTIONS
        ]
        results = reciprocal_rank_fusion(result_lists, top_k=top_k)
    else:
        results = await retrieval_service.search(
            session, query, filters, top_k, collection, include_metadata=True
        )
    if since is None:
        return results
    return [chunk for chunk in results if _passes_recency_filter(chunk, since)]


def _passes_recency_filter(chunk: ScoredChunk, since: datetime) -> bool:
    """See `search_recent_changes`'s docstring for why this is best-effort."""
    for key in _RECENCY_METADATA_KEYS:
        raw_value = chunk.metadata.get(key)
        if not raw_value:
            continue
        try:
            timestamp = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return timestamp >= since
    return True


async def get_question_history(
    session: AsyncSession,
    actor: Identity,
    *,
    limit: int = 20,
    offset: int = 0,
) -> list[AgentExecution]:
    """Return `actor`'s own past `agent_executions` rows, newest first --
    backs `GET /ask/history`. Requires `actor.user_id` (populated only for
    `kind == USER` identities, per `Identity`'s own docstring): an MCP or
    service caller has no per-user history to return, so that case is a hard
    `PermissionDeniedError` rather than a silently empty list, which would
    look identical to "you really have asked nothing yet."
    """
    if actor.user_id is None:
        raise PermissionDeniedError(
            "Question history is only available to authenticated users.",
            error_code="agents.history_requires_user",
        )
    rows = await repository.list_agent_executions_for_user(
        session,
        user_id=actor.user_id,
        organization_id=actor.organization_id,
        limit=limit,
        offset=offset,
    )
    return [AgentExecution.model_validate(row) for row in rows]


def _gap_report_to_schema(row: KnowledgeGapReport) -> GapReport:
    return GapReport(
        id=row.id,
        organization_id=row.organization_id,
        suggested_topic=row.suggested_topic,
        supporting_execution_ids=[uuid.UUID(value) for value in row.supporting_execution_ids],
        suggested_action=row.suggested_action,
        related_document_id=row.related_document_id,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def detect_knowledge_gaps(
    session: AsyncSession,
    actor: Identity,
    *,
    trigger_source: TriggerSource = "scheduled",
) -> list[GapReport]:
    """Run the Knowledge Gap Agent for `actor.organization_id`
    (`agents.knowledge_gap.pipeline.detect_knowledge_gaps`) and record one
    `agent_executions` row for the run itself (DATABASE_DESIGN.md's uniform
    "one row per agent run" convention -- this agent's own execution is as
    observable as any other, even though it runs on a schedule rather than
    per-question).

    Takes `actor: Identity` like every other entry point in this module
    (API_DESIGN.md: "Identity is threaded through every call"), not a bare
    `organization_id` -- the only caller, `app.agents.workers.tasks`'s cron
    job, constructs `Identity.for_agent("knowledge_gap_agent",
    organization_id)` per organization, mirroring
    `core.tenancy.service.update_connector_sync_status`'s identical
    precedent for a scheduled worker's system-triggered identity.

    `trigger_source` defaults to `"scheduled"` (not `"core_api"`, unlike
    every other entry point in this module): there is no REST/MCP action
    that triggers a fresh run on demand, only reads the results
    (`list_gap_reports`), per AGENT_WORKFLOWS.md section 2.6's "not part of
    the per-question flow."

    No two-tier failure handling here, matching `generate_postmortem`'s
    reasoning, not `answer_question`'s: a `GapReport` is a real
    recommendation a human may act on, not a disposable chat answer, so a
    failure here is marked `failed` and re-raised rather than papered over
    with a fabricated empty result.
    """
    await cost_budget.check_cost_budget(session, actor.organization_id)

    settings = get_settings()
    execution = await repository.insert_agent_execution(
        session,
        organization_id=actor.organization_id,
        agent_name="detect_knowledge_gaps",
        trigger_source=trigger_source,
        input_summary={"organization_id": str(actor.organization_id)},
    )

    usage_handler = UsageMetadataCallbackHandler()

    try:
        llm = get_llm().with_config(callbacks=[usage_handler])
        rows = await _run_knowledge_gap_pipeline(
            session,
            llm,
            actor.organization_id,
            confidence_threshold=settings.confidence_threshold,
            lookback=timedelta(days=settings.knowledge_gap_lookback_days),
            min_cluster_size=settings.knowledge_gap_min_cluster_size,
            similarity_threshold=settings.knowledge_gap_similarity_threshold,
        )
    except Exception as exc:
        await repository.update_agent_execution(
            session,
            execution.id,
            status="failed",
            error_detail=str(exc)[:2000],
            completed_at=datetime.now(UTC),
            **summarize_usage(usage_handler),
        )
        raise

    await repository.update_agent_execution(
        session,
        execution.id,
        status="succeeded",
        completed_at=datetime.now(UTC),
        **summarize_usage(usage_handler),
    )
    return [_gap_report_to_schema(row) for row in rows]


async def list_gap_reports(session: AsyncSession, actor: Identity) -> list[GapReport]:
    """List every currently-open gap report for `actor.organization_id`
    (API_DESIGN.md: `GET /knowledge/gaps`) -- a pure read, no agent run
    triggered, no `agent_executions` row recorded.

    Gated by `knowledge:review`, the same permission
    `core.knowledge.service.list_proposed_documents` requires -- gap
    reports surface the same kind of oversight information (what's
    under-documented, per PROJECT_PLAN.md's "Documentation/platform owner"
    persona) as the proposed-documents review queue, not something every
    employee should see by default.
    """
    require_permission(actor, _GAP_REVIEW_PERMISSION)
    rows = await knowledge_gap_repository.list_open_gap_reports(session, actor.organization_id)
    return [_gap_report_to_schema(row) for row in rows]


async def get_agent_execution_stats(
    session: AsyncSession, actor: Identity, *, since: datetime | None = None
) -> list[AgentExecutionStats]:
    """Per-agent execution/latency/confidence aggregate for
    `actor.organization_id` -- Milestone 10's observability-dashboard
    requirement (PROJECT_PLAN.md section 10), the `agent_executions`-side
    counterpart to `core.observability.service.get_mcp_dashboard`.

    Gated by `observability:read` (a new permission code, duplicated here
    rather than imported from `core.observability.service` -- matching this
    codebase's existing convention of each module owning its own permission
    constant even when the string value is shared, e.g. `_GAP_REVIEW_
    PERMISSION` above vs. `core.knowledge.service._REVIEW_PERMISSION`, both
    `"knowledge:review"`).

    Phase 5.7's `estimated_cost_usd` is computed against `Settings.
    agent_llm_model` (the *currently configured* model), applied to each
    group's total token counts -- not a true per-execution, per-model-at-
    the-time breakdown. This is a deliberate simplification: `app.agents.
    llm.get_llm()` is a single global model setting that changes rarely, so
    the overwhelming majority of any group's captured tokens were spent
    against whatever model is configured right now. A historical model
    change would make this estimate approximate for that window, not wrong
    by orders of magnitude -- flagged here rather than silently assumed
    away, and not solved with a heavier per-model-breakdown query since
    nothing in this codebase's actual usage pattern needs that precision yet.
    """
    require_permission(actor, _OBSERVABILITY_READ_PERMISSION)
    rows = await repository.get_agent_execution_stats(
        session, actor.organization_id, since=since
    )
    current_model = get_settings().agent_llm_model
    return [
        AgentExecutionStats(
            agent_name=row.agent_name,
            execution_count=row.execution_count,
            succeeded_count=int(row.succeeded_count or 0),
            failed_count=int(row.failed_count or 0),
            avg_confidence_score=(
                float(row.avg_confidence_score) if row.avg_confidence_score is not None else None
            ),
            avg_latency_seconds=(
                float(row.avg_latency_seconds) if row.avg_latency_seconds is not None else None
            ),
            total_prompt_tokens=(
                int(row.total_prompt_tokens) if row.total_prompt_tokens is not None else None
            ),
            total_completion_tokens=(
                int(row.total_completion_tokens)
                if row.total_completion_tokens is not None
                else None
            ),
            total_tokens=int(row.total_tokens) if row.total_tokens is not None else None,
            estimated_cost_usd=get_estimated_cost_usd(
                current_model,
                int(row.total_prompt_tokens) if row.total_prompt_tokens is not None else None,
                int(row.total_completion_tokens)
                if row.total_completion_tokens is not None
                else None,
            ),
        )
        for row in rows
    ]


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

    Phase 6.6: checks `cost_budget.check_cost_budget` before recording
    anything or invoking `graph` at all -- an organization that has already
    exceeded its configured daily budget should not have a further
    `agent_executions` row created for an attempt that's about to be
    refused anyway. `CostBudgetExceededError` is an expected domain error
    (like `PermissionDeniedError`), so it's allowed to propagate here, not
    caught by the two-tier handling below -- the caller must see a real
    429, never a fabricated degraded answer that looks like a normal
    low-confidence response.
    """
    await cost_budget.check_cost_budget(session, initial_state.actor.organization_id)

    execution = await repository.insert_agent_execution(
        session,
        organization_id=initial_state.actor.organization_id,
        agent_name=agent_name,
        trigger_source=trigger_source,
        user_id=initial_state.actor.user_id,
        input_summary=input_summary,
    )

    # Phase 5.4/5.7: attached once, at the single top-level `graph.ainvoke`
    # call -- LangChain propagates callbacks to every LLM call any node
    # makes during this run (query rewriting, answer generation, grounding,
    # sufficiency, hypothesis generation), so this captures the whole
    # execution's real token usage without each node needing its own
    # instrumentation. See `app.agents.telemetry.summarize_usage`'s own
    # docstring for why a partial/zero result here is never treated as "zero
    # tokens spent" -- only as "not captured."
    usage_handler = UsageMetadataCallbackHandler()

    try:
        raw_final_state = await graph.ainvoke(initial_state, config={"callbacks": [usage_handler]})
    except EKIPError as exc:
        await repository.update_agent_execution(
            session,
            execution.id,
            status="failed",
            error_detail=str(exc)[:2000],
            completed_at=datetime.now(UTC),
            **summarize_usage(usage_handler),
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
            completed_at=datetime.now(UTC),
            **summarize_usage(usage_handler),
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
            completed_at=datetime.now(UTC),
            **summarize_usage(usage_handler),
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
        completed_at=datetime.now(UTC),
        **summarize_usage(usage_handler),
    )
    return final_state.result
