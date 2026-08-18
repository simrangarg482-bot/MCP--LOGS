"""Query understanding/rewriting -- stage 1 of the Retrieval Agent
(AGENT_WORKFLOWS.md section 2.1 step 1 / PROJECT_PLAN.md section 6.1).

Owned by: agents/retrieval/. Resolves vague, context-dependent references
(e.g. "this error") into the actual incident's description text, and
expands abbreviations -- but only calls the LLM when there's a real reason
to: an `incident_id` is present (there's context to resolve against) or the
query itself looks vague. A query with neither is passed through unchanged,
per AGENT_WORKFLOWS.md's own framing: "a cheap heuristic, not a separate LLM
call, to avoid unnecessary latency/cost on already-clear queries."

Reads `core.incidents` (`get_incident`) to resolve `incident_id` into
concrete description text -- within PROJECT_PLAN.md section 9.7's documented
dependency list (`retrieval`, `core`, `shared`), unlike several of
ingestion's undocumented-but-necessary reads earlier in this project.
"""

from __future__ import annotations

import uuid

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.prompt_safety import build_messages
from app.agents.retry import call_with_retry
from app.core.incidents import service as incidents_service
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity

logger = get_logger(__name__)

# A deliberately small, literal set of vague-reference phrases -- the docs'
# own example ("this error") belongs to this set. A cheap heuristic, not an
# attempt at general ambiguity detection (AGENT_WORKFLOWS.md section 2.1).
_VAGUE_REFERENCE_TERMS = (
    "this error",
    "that error",
    "this issue",
    "that issue",
    "this problem",
    "that problem",
    "this bug",
    "that bug",
    "this failure",
    "the above",
    "same issue",
)


def _looks_vague(query: str) -> bool:
    lowered = query.lower()
    return any(term in lowered for term in _VAGUE_REFERENCE_TERMS)


async def rewrite_query(
    session: AsyncSession,
    *,
    query: str,
    incident_id: uuid.UUID | None,
    actor: Identity,
    llm: BaseChatModel,
    retry_count: dict[str, int],
) -> str:
    """Return the rewritten query, or `query` unchanged when rewriting is
    skipped or fails (see module docstring).

    A rewriting failure (LLM timeout/rate-limit, exhausted per
    `agents.retry.call_with_retry`) degrades to the original `query` rather
    than failing the whole Retrieval Agent node -- "no query understanding
    happened" is a worse retrieval, not a broken one, and this stage has no
    documented terminal-error behavior of its own in AGENT_WORKFLOWS.md
    section 2.1 (only the hybrid-retrieval stage does).
    """
    incident_description: str | None = None
    if incident_id is not None:
        # Deliberately not wrapped in the same try/except as the LLM call
        # below: a `NotFoundError`/`PermissionDeniedError` here means the
        # caller passed a bad or foreign `incident_id` -- an input-validation
        # problem, not a transient failure this stage should silently
        # degrade around. It propagates to the graph-level exception
        # handling AGENT_WORKFLOWS.md section 4 describes for "truly
        # unexpected exceptions."
        incident = await incidents_service.get_incident(
            session, actor, actor.organization_id, incident_id
        )
        incident_description = incident.description

    if incident_id is None and not _looks_vague(query):
        return query

    messages = _build_prompt(query, incident_description)

    try:
        response = await call_with_retry(
            "retrieval_agent.rewrite_query",
            lambda: llm.ainvoke(messages),
            retry_count=retry_count,
        )
    except Exception as exc:
        logger.warning("query_rewrite_failed_using_original", query=query, error=str(exc))
        return query

    rewritten_text = str(response.content).strip()
    return rewritten_text or query


def _build_prompt(query: str, incident_description: str | None) -> list[BaseMessage]:
    if incident_description is not None:
        # `incident_description` is human-authored, org-internal text but not
        # EKIP-authored -- fenced as evidence via `build_messages` the same
        # as every other agents/ prompt that mixes fixed instructions with
        # content the caller doesn't control.
        return build_messages(
            system_instructions=(
                "Rewrite the following question into a self-contained, specific "
                "search query. Expand any abbreviations. Resolve vague "
                "references (e.g. 'this error', 'that issue') using the incident "
                "context below. Return only the rewritten query, no "
                "explanation."
            ),
            evidence_block=f"Incident context: {incident_description}",
            task=f"Question: {query}",
        )
    return [
        SystemMessage(
            content=(
                "Rewrite the following question into a self-contained, specific "
                "search query. Expand any abbreviations. Return only the rewritten "
                "query, no explanation."
            )
        ),
        HumanMessage(content=f"Question: {query}"),
    ]
