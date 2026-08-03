"""Investigation Agent Sub-stage A: evidence gathering -- collection only, no
interpretation (AGENT_WORKFLOWS.md section 2.4 / PROJECT_PLAN.md section
6.4). Sub-stage B (`agents.investigation.hypothesis`, task #23) is the only
place raw evidence becomes an AI-generated conclusion -- keeping the two
sub-stages in separate modules, not just separate prompt sections, is what
makes "verified vs. AI-generated" structural (API_DESIGN.md's own framing),
not something a shared function could accidentally blur together.

Searches, in priority order, short-circuiting once `_EVIDENCE_CAP` items are
gathered (AGENT_WORKFLOWS.md section 2.4: "short-circuiting once sufficient
evidence is found, to bound latency"):

  1. GitHub evidence (files, commits, pull requests, issues) --
     `retrieval.service.search(..., collection="code")` for file chunks plus
     `retrieval.service.search(..., collection="documentation")` for commit/
     PR/issue chunks (see below for why these land in two different
     collections), both with `include_metadata=True`.
  2. Slack conversations -- `retrieval.service.search(..., collection="conversations")`.
  3. Jira/Azure DevOps tickets -- no connector for either exists yet
     (PROJECT_PLAN.md Milestone 9), so nothing is ingested to search; this
     source always contributes zero evidence today. A real, flagged gap,
     not a bug.
  4. Existing postmortems -- `core.incidents.list_recent_postmortems`, since
     no "postmortems" retrieval collection exists (`retrieval_models.py`'s
     own flagged gap) to search by relevance instead.
  5. Monitoring/alert metadata -- explicitly mocked per AGENT_WORKFLOWS.md
     section 2.4 ("interface designed so a real integration can replace the
     mock without changing the graph"). Returns empty: `EvidenceItem.source`
     itself (API_DESIGN.md's vocabulary) has no "monitoring"/"alert" value,
     only `deployment` among the non-code/chat/ticket options -- fabricating
     a placeholder evidence item under a mismatched source label would be
     worse than an honestly empty result.

The GitHub connector (`ingestion.connectors.github`) ingests files, commits,
pull requests, and issues (extended from files-only). File content still
classifies as `ContentType="code"` (recognized extension) and lands in the
"code" collection, unchanged. Commit messages/PR bodies/issue bodies+comments
are prose with no code-file extension for `classify_content_type` to key
off of, so they classify as `ContentType="document"` and land in the
"documentation" collection instead -- reusing that existing collection
rather than creating a new "github_events"-style one, per this feature's
"reuse the current pipeline wherever possible" constraint. `_gather_code_evidence`
therefore searches both collections and merges the results, filtering the
"documentation" side down to chunks whose metadata carries a `repo` key
(i.e., actually GitHub-sourced), since that collection could in principle
also hold non-GitHub documentation once a Confluence/SharePoint connector
exists. Each chunk's `source` is derived from its own `metadata["kind"]`
(`"commit"` / `"pull_request"` / `"issue"`; a plain file chunk has no
`"kind"` key at all and defaults to `"github"`) -- see `_chunk_to_evidence`.

Each retrieval-backed source's call is retried per
`agents.retry.call_with_retry`; a source that still fails after retries is
logged and skipped (contributes zero evidence), never fatal to the whole
gather -- AGENT_WORKFLOWS.md section 2.4's own failure-handling rule
("an individual source failing ... is logged and skipped, not treated as
fatal").
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.retry import call_with_retry
from app.core.incidents import service as incidents_service
from app.retrieval import service as retrieval_service
from app.retrieval.schemas import ScoredChunk, SearchFilters
from app.shared.config.logging import get_logger
from app.shared.schemas import EvidenceItem, Identity

logger = get_logger(__name__)

# Stop gathering once this many evidence items are collected -- a
# placeholder cap (AGENT_WORKFLOWS.md leaves "sufficient evidence"
# undefined), generous enough that sub-stage B still has real material to
# reason over without unbounded latency.
_EVIDENCE_CAP = 10
_PER_SOURCE_TOP_K = 5
_EXCERPT_MAX_CHARS = 300


async def gather_evidence(
    session: AsyncSession, *, query: str, actor: Identity, retry_count: dict[str, int]
) -> list[EvidenceItem]:
    """Collect evidence for `query`, in priority order, stopping once
    `_EVIDENCE_CAP` items are gathered.
    """
    evidence: list[EvidenceItem] = []
    filters = SearchFilters(organization_id=actor.organization_id, permission_codes=actor.permissions)

    evidence.extend(await _gather_code_evidence(session, query, filters, retry_count))

    if len(evidence) < _EVIDENCE_CAP:
        evidence.extend(await _gather_slack_evidence(session, query, filters, retry_count))

    if len(evidence) < _EVIDENCE_CAP:
        evidence.extend(_gather_jira_evidence())

    if len(evidence) < _EVIDENCE_CAP:
        evidence.extend(await _gather_postmortem_evidence(session, actor, retry_count))

    if len(evidence) < _EVIDENCE_CAP:
        evidence.extend(_gather_monitoring_evidence())

    return evidence[:_EVIDENCE_CAP]


async def _gather_code_evidence(
    session: AsyncSession, query: str, filters: SearchFilters, retry_count: dict[str, int]
) -> list[EvidenceItem]:
    """Searches "code" (file chunks) and "documentation" (commit/PR/issue
    chunks) together as one logical "GitHub evidence" source -- see module
    docstring for why both collections are involved. Results are merged and
    re-sorted by score before truncating to `_PER_SOURCE_TOP_K` combined,
    rather than taking `_PER_SOURCE_TOP_K` from each collection and letting
    this source's contribution to `_EVIDENCE_CAP` silently double.
    """
    try:
        code_chunks, doc_chunks = await call_with_retry(
            "investigation_agent.evidence.code",
            lambda: _search_code_and_documentation(session, query, filters),
            retry_count=retry_count,
        )
    except Exception as exc:
        logger.warning("investigation_evidence_source_failed", source="code", error=str(exc))
        return []

    github_doc_chunks = [chunk for chunk in doc_chunks if chunk.metadata.get("repo")]
    combined = sorted(code_chunks + github_doc_chunks, key=lambda chunk: chunk.score, reverse=True)
    return [_chunk_to_evidence(chunk) for chunk in combined[:_PER_SOURCE_TOP_K]]


async def _search_code_and_documentation(
    session: AsyncSession, query: str, filters: SearchFilters
) -> tuple[list[ScoredChunk], list[ScoredChunk]]:
    """The two collection searches `_gather_code_evidence` needs, run under
    one shared retry attempt (matching this module's "one source, one
    `call_with_retry`" convention rather than tracking the two collections
    as separately-retried sources).
    """
    code_chunks = await retrieval_service.search(
        session, query, filters, _PER_SOURCE_TOP_K, collection="code", include_metadata=True
    )
    doc_chunks = await retrieval_service.search(
        session, query, filters, _PER_SOURCE_TOP_K, collection="documentation", include_metadata=True
    )
    return code_chunks, doc_chunks


async def _gather_slack_evidence(
    session: AsyncSession, query: str, filters: SearchFilters, retry_count: dict[str, int]
) -> list[EvidenceItem]:
    try:
        chunks = await call_with_retry(
            "investigation_agent.evidence.slack",
            lambda: retrieval_service.search(
                session, query, filters, _PER_SOURCE_TOP_K, collection="conversations"
            ),
            retry_count=retry_count,
        )
    except Exception as exc:
        logger.warning("investigation_evidence_source_failed", source="slack", error=str(exc))
        return []
    return [_chunk_to_evidence(chunk, source="slack") for chunk in chunks]


def _gather_jira_evidence() -> list[EvidenceItem]:
    """No Jira/Azure DevOps connector exists yet (PROJECT_PLAN.md Milestone
    9) -- nothing is ingested for either source, so there is nothing to
    search. Kept as its own function (rather than simply omitted) so the
    gap is visible in the call sequence, and so a future connector only
    needs to fill this function in, not restructure `gather_evidence`.
    """
    return []


async def _gather_postmortem_evidence(
    session: AsyncSession, actor: Identity, retry_count: dict[str, int]
) -> list[EvidenceItem]:
    try:
        postmortems = await call_with_retry(
            "investigation_agent.evidence.postmortems",
            lambda: incidents_service.list_recent_postmortems(
                session, actor, actor.organization_id, limit=_PER_SOURCE_TOP_K
            ),
            retry_count=retry_count,
        )
    except Exception as exc:
        logger.warning("investigation_evidence_source_failed", source="postmortem", error=str(exc))
        return []

    now = datetime.now(timezone.utc)
    return [
        EvidenceItem(
            source="postmortem",
            reference=f"postmortem:{postmortem.id}",
            summary=(postmortem.root_cause or "(no root cause recorded)")[:_EXCERPT_MAX_CHARS],
            retrieved_at=now,
        )
        for postmortem in postmortems
    ]


def _gather_monitoring_evidence() -> list[EvidenceItem]:
    """Mocked per AGENT_WORKFLOWS.md section 2.4 -- see module docstring on
    why this returns empty rather than a fabricated placeholder: no real
    monitoring/alerting integration exists, and `EvidenceItem.source`'s own
    vocabulary has no dedicated value for it either.
    """
    return []


# Maps `ScoredChunk.metadata["kind"]` (set by `ingestion.connectors.github`
# on commit/pull_request/issue documents only -- see that module's
# docstring) to the matching `EvidenceItem.source` value. A file chunk has
# no `"kind"` key at all, so `.get("kind", "")` falls through to the
# `_chunk_to_evidence` default of `"github"` rather than needing an entry
# here.
_KIND_TO_SOURCE: dict[str, Literal["commit", "pull_request", "issue"]] = {
    "commit": "commit",
    "pull_request": "pull_request",
    "issue": "issue",
}


def _chunk_to_evidence(
    chunk: ScoredChunk,
    *,
    source: Literal["github", "commit", "pull_request", "issue", "slack"] | None = None,
) -> EvidenceItem:
    """Convert one retrieved `ScoredChunk` into an `EvidenceItem`.

    `source` is an explicit override for callers that already know it
    outright (`_gather_slack_evidence` always passes `source="slack"`, since
    Slack chunks carry no `"kind"` metadata to derive it from). Left as
    `None`, `source` is derived from `chunk.metadata["kind"]` via
    `_KIND_TO_SOURCE` -- this is how `_gather_code_evidence` tells a plain
    GitHub file chunk (`source="github"`, the fallback) apart from a commit/
    PR/issue chunk without needing to know which collection the chunk came
    from.

    `reference` prefers the chunk's real `source_url`; falls back to a
    synthetic `chunk:<id>` reference for chunks with none (e.g. a Slack
    message whose connector never resolved a permalink -- see
    `ingestion.connectors.slack`'s own flagged limitation). `retrieved_at` is
    when this evidence-gathering step ran; `source_timestamp` is the
    original GitHub object's own date (`chunk.metadata["timestamp"]` -- a
    commit's authored date, a PR's/issue's `created_at`), parsed when
    present and parseable, `None` otherwise (matching a plain file chunk,
    which carries no such metadata key). `metadata` carries every kind-
    specific fact the connector attached (`author`/`labels`/`changed_files`/
    `reviews`/...) through verbatim, structured, rather than folding them
    into `summary`'s prose -- see `EvidenceItem.metadata`'s own docstring.
    """
    resolved_source = source or _KIND_TO_SOURCE.get(chunk.metadata.get("kind", ""), "github")
    reference = chunk.source_url or f"chunk:{chunk.chunk_id}"
    summary = chunk.content[:_EXCERPT_MAX_CHARS]
    if len(chunk.content) > _EXCERPT_MAX_CHARS:
        summary += "..."
    return EvidenceItem(
        source=resolved_source,
        reference=reference,
        summary=summary,
        retrieved_at=datetime.now(timezone.utc),
        source_timestamp=_parse_source_timestamp(chunk.metadata.get("timestamp")),
        metadata=chunk.metadata,
    )


def _parse_source_timestamp(value: str | None) -> datetime | None:
    """Parse a GitHub-connector timestamp metadata value (ISO 8601,
    `Z`-suffixed UTC -- see `ingestion.connectors.github`) into an aware
    `datetime`, or `None` if `value` is missing/empty/unparseable. Never
    raises: a malformed timestamp should degrade to "unknown", not fail the
    whole evidence-gathering step.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
