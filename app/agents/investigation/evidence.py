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
  2. Org knowledge/runbooks -- `retrieval.service.search(...,
     collection="documentation")`, the complementary (non-`repo`-tagged)
     side of the same collection source 1 restricts to GitHub content: any
     manually-proposed-and-published document (`core.knowledge.service.
     publish_document`) or ingested runbooks-connector content lands here
     instead -- see `_gather_knowledge_evidence`.
  3. Slack conversations -- `retrieval.service.search(..., collection="conversations")`.
  4. Jira/Azure DevOps tickets -- no connector for either exists yet
     (PROJECT_PLAN.md Milestone 9), so nothing is ingested to search; this
     source always contributes zero evidence today. A real, flagged gap,
     not a bug.
  5. Existing postmortems -- `core.incidents.list_recent_postmortems`, since
     no "postmortems" retrieval collection exists (`retrieval_models.py`'s
     own flagged gap) to search by relevance instead. Deduplicated against
     source 2's runbook evidence first (`_gather_postmortem_evidence`'s own
     docstring): once a postmortem has been ingested by
     `ingestion.connectors.runbooks` (P1) and is already represented as a
     `"runbook"` evidence item, it is not also returned here as a second,
     redundant `"postmortem"` item for the same underlying knowledge.
  6. Monitoring/alert metadata -- explicitly mocked per AGENT_WORKFLOWS.md
     section 2.4 ("interface designed so a real integration can replace the
     mock without changing the graph"). Returns empty: `EvidenceItem.source`
     itself (API_DESIGN.md's vocabulary) has no "monitoring"/"alert" value,
     only `deployment` among the non-code/chat/ticket options -- fabricating
     a placeholder evidence item under a mismatched source label would be
     worse than an honestly empty result.
  7. Live evidence (`agents.investigation.live/`) -- optional, and only
     reached after the six sources above and only when
     `_should_augment_with_live_evidence` says the indexed evidence so far
     is thin, stale, or this is a real, active incident (see that function's
     docstring). Fetches directly from GitHub's/Slack's APIs, scoped to the
     org's already-registered `connector_configs`, rather than from
     anything already ingested -- see `_gather_live_evidence`.

This module's evidence gathering is deliberately hybrid, not purely
ingestion-backed and not purely live: sub-stage A's first six sources
(above) search what has already been indexed -- fast, and (per
`ingestion.workers.main`'s hourly `scheduled_reconciliation` cron) at most
about an hour stale. Live evidence exists specifically to cover the gap
between "an hour ago's last sync" and "right now" for a genuinely active
incident investigation, without turning the Investigation Agent into a
second ingestion pipeline: it performs a handful of narrow, targeted,
un-paginated API calls per investigation and returns `EvidenceItem`s
directly (see `agents.investigation.live.base.LiveEvidenceSource`) -- it
never persists anything, never produces a `RawDocument`, and never touches
`app.ingestion` (which `agents/` is forbidden from importing at all, per
this project's import-linter contract). Ingestion remains the sole
long-term knowledge-synchronization mechanism; live evidence is a
narrowly-scoped, read-only supplement used only while actively
investigating.

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
(i.e., actually GitHub-sourced), since that collection also holds non-GitHub
documentation -- manually-published runbooks today, and once a Confluence/
SharePoint connector exists, that too. `_gather_knowledge_evidence` (source
2 above) is the complementary consumer: it searches the same collection for
chunks *without* a `repo` key, so the two sources partition it rather than
double-counting. Each chunk's `source` is derived from its own `metadata["kind"]`
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

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.investigation.live.base import LiveEvidenceSource
from app.agents.investigation.live.github_live import GitHubLiveSource
from app.agents.investigation.live.monitoring_live import MonitoringLiveSource
from app.agents.investigation.live.slack_live import SlackLiveSource
from app.agents.retry import call_with_retry
from app.core.incidents import service as incidents_service
from app.core.tenancy import service as tenancy_service
from app.retrieval import service as retrieval_service
from app.retrieval.schemas import ScoredChunk, SearchFilters
from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings
from app.shared.schemas import EvidenceItem, Identity

logger = get_logger(__name__)

# Stop gathering once this many evidence items are collected -- a
# placeholder cap (AGENT_WORKFLOWS.md leaves "sufficient evidence"
# undefined), generous enough that sub-stage B still has real material to
# reason over without unbounded latency.
_EVIDENCE_CAP = 10
_PER_SOURCE_TOP_K = 5
_EXCERPT_MAX_CHARS = 300

# Keyed by `app.core.tenancy.schemas.ConnectorSource` -- only sources with a
# real `LiveEvidenceSource` implementation appear here. `_gather_live_evidence`
# simply skips any registered connector whose `source` has no entry (e.g.
# `"jira"`/`"confluence"`, matching this module's existing, honestly-empty
# `_gather_jira_evidence`). `"monitoring"` was added once a `ConnectorSource`
# value for it existed (`app.core.tenancy.schemas.ConnectorSource`) --
# `MonitoringLiveSource.fetch_live_evidence` itself still always returns
# `[]` (no real monitoring/alerting backend exists yet), but a
# `connector_configs` row with `source="monitoring"` now dispatches here
# instead of being silently skipped like an unregistered source would be.
_LIVE_SOURCES: dict[str, LiveEvidenceSource] = {
    "github": GitHubLiveSource(),
    "slack": SlackLiveSource(),
    "monitoring": MonitoringLiveSource(),
}

# Below this many indexed evidence items, treat the knowledge base's answer
# as too thin to skip live augmentation -- a placeholder threshold in the
# same spirit as `_EVIDENCE_CAP` (AGENT_WORKFLOWS.md leaves "sufficient
# evidence" undefined generally, not just for the overall cap).
_LIVE_EVIDENCE_MIN_COUNT = 3
# If the freshest *timestamped* indexed evidence found is older than this,
# treat it as possibly stale relative to "right now" -- deliberately wider
# than the hourly ingestion reconciliation cadence
# (`ingestion.workers.main.scheduled_reconciliation`), so this only fires
# when the indexed data genuinely might be behind, not on every call.
_LIVE_EVIDENCE_FRESHNESS_WINDOW = timedelta(hours=2)


async def gather_evidence(
    session: AsyncSession,
    *,
    query: str,
    actor: Identity,
    retry_count: dict[str, int],
    incident_id: uuid.UUID | None = None,
) -> list[EvidenceItem]:
    """Collect evidence for `query`, in priority order, stopping once
    `_EVIDENCE_CAP` items are gathered.

    `incident_id` is optional (default `None`, preserving every pre-existing
    caller's behavior unchanged) -- passed by `investigation.node` as
    `state.incident_id` when this investigation is genuinely attached to a
    real incident (always true for `triage_incident`; optionally true for an
    incident-scoped `answer_question` call). It is one of
    `_should_augment_with_live_evidence`'s three signals for whether live
    evidence is worth fetching -- see that function's docstring.
    """
    evidence: list[EvidenceItem] = []
    filters = SearchFilters(organization_id=actor.organization_id, permission_codes=actor.permissions)

    evidence.extend(await _gather_code_evidence(session, query, filters, retry_count))

    if len(evidence) < _EVIDENCE_CAP:
        evidence.extend(await _gather_knowledge_evidence(session, query, filters, retry_count))

    if len(evidence) < _EVIDENCE_CAP:
        evidence.extend(await _gather_slack_evidence(session, query, filters, retry_count))

    if len(evidence) < _EVIDENCE_CAP:
        evidence.extend(_gather_jira_evidence())

    if len(evidence) < _EVIDENCE_CAP:
        evidence.extend(await _gather_postmortem_evidence(session, actor, retry_count, evidence))

    if len(evidence) < _EVIDENCE_CAP:
        evidence.extend(_gather_monitoring_evidence())

    if (
        len(evidence) < _EVIDENCE_CAP
        and get_settings().investigation_live_evidence_enabled
        and _should_augment_with_live_evidence(evidence, incident_id)
    ):
        evidence.extend(await _gather_live_evidence(session, query, actor, retry_count))

    return evidence[:_EVIDENCE_CAP]


def _should_augment_with_live_evidence(
    evidence: list[EvidenceItem], incident_id: uuid.UUID | None
) -> bool:
    """Decide whether the indexed evidence gathered so far is worth
    supplementing with live external lookups -- the hybrid design's actual
    trigger. True if any of:

      - `incident_id is not None`: this is a genuinely active incident
        investigation (not just a low-confidence general question), so
        freshness always matters. Note this is deliberately *not* gated on
        `GraphState.confidence_score`: by the time this function ever runs,
        confidence was either already established as too low (the
        confidence-routed `answer_question` path -- the Investigation Agent
        is never reached otherwise) or never computed at all
        (`triage_incident`'s dedicated graph skips Retrieval/Confidence
        Evaluation entirely -- see `agents.graph`'s module docstring).
        Checking confidence here would either always be true or never
        available; `incident_id` is the one signal this function can
        actually act on.
      - Fewer than `_LIVE_EVIDENCE_MIN_COUNT` indexed items were found --
        the knowledge base's answer is thin regardless of recency.
      - The freshest *timestamped* indexed item (only GitHub-kind evidence
        carries `source_timestamp` today -- see `_chunk_to_evidence`) is
        older than `_LIVE_EVIDENCE_FRESHNESS_WINDOW`, or nothing gathered
        carries a timestamp at all (treated the same way -- absence of
        freshness information is not evidence *of* freshness, so this
        defaults toward augmenting rather than silently skipping).
    """
    if incident_id is not None:
        return True

    if len(evidence) < _LIVE_EVIDENCE_MIN_COUNT:
        return True

    timestamps = [item.source_timestamp for item in evidence if item.source_timestamp is not None]
    if not timestamps:
        return True

    newest = max(timestamps)
    return datetime.now(UTC) - newest > _LIVE_EVIDENCE_FRESHNESS_WINDOW


async def _gather_live_evidence(
    session: AsyncSession, query: str, actor: Identity, retry_count: dict[str, int]
) -> list[EvidenceItem]:
    """Fetch fresh evidence directly from external APIs (GitHub, Slack),
    scoped to `actor`'s organization's already-registered `connector_configs`
    -- the live half of this module's hybrid design (see module docstring).

    Resolves connector configs via `core.tenancy.service.list_connectors`
    (never `app.ingestion`, which `agents/` may not import at all) and
    dispatches each `status == "active"` one to the matching
    `_LIVE_SOURCES[connector_config.source]`, if any. Each source's call is
    retried per `call_with_retry`/logged-and-skipped on failure, exactly
    like every other source in this module -- one connector's live lookup
    failing (a bad token, a rate limit, a timeout) does not affect any
    other connector's.
    """
    try:
        connector_configs = await tenancy_service.list_connectors(
            session, actor, actor.organization_id
        )
    except Exception as exc:
        logger.warning("investigation_evidence_source_failed", source="live", error=str(exc))
        return []

    since = datetime.now(UTC) - timedelta(
        hours=get_settings().investigation_live_evidence_lookback_hours
    )

    evidence: list[EvidenceItem] = []
    for connector_config in connector_configs:
        if connector_config.status != "active":
            continue
        live_source = _LIVE_SOURCES.get(connector_config.source)
        if live_source is None:
            continue

        try:
            items = await call_with_retry(
                f"investigation_agent.evidence.live.{connector_config.source}",
                lambda live_source=live_source, connector_config=connector_config: (
                    live_source.fetch_live_evidence(
                        connector_config=connector_config,
                        query=query,
                        since=since,
                        limit=_PER_SOURCE_TOP_K,
                    )
                ),
                retry_count=retry_count,
            )
        except Exception as exc:
            logger.warning(
                "investigation_evidence_source_failed",
                source=f"live_{connector_config.source}",
                error=str(exc),
            )
            continue
        evidence.extend(items)

    return evidence


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


async def _gather_knowledge_evidence(
    session: AsyncSession, query: str, filters: SearchFilters, retry_count: dict[str, int]
) -> list[EvidenceItem]:
    """Searches the "documentation" collection for org knowledge/runbooks --
    the complementary side of the collection `_gather_code_evidence`
    restricts to `repo`-tagged (GitHub) chunks. Covers both manually-
    proposed-and-published documents (`core.knowledge.service.
    publish_document` embeds the whole document as one chunk at publish
    time) and, once ingested, runbooks-connector content
    (`ingestion.connectors.runbooks`) -- neither carries a `repo` metadata
    key, so filtering it out here is what keeps this source and
    `_gather_code_evidence` from double-counting the same collection.
    """
    try:
        chunks = await call_with_retry(
            "investigation_agent.evidence.knowledge",
            lambda: retrieval_service.search(
                session, query, filters, _PER_SOURCE_TOP_K, collection="documentation", include_metadata=True
            ),
            retry_count=retry_count,
        )
    except Exception as exc:
        logger.warning("investigation_evidence_source_failed", source="knowledge", error=str(exc))
        return []
    non_github_chunks = [chunk for chunk in chunks if not chunk.metadata.get("repo")]
    return [_chunk_to_evidence(chunk, source="runbook") for chunk in non_github_chunks]


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
    session: AsyncSession,
    actor: Identity,
    retry_count: dict[str, int],
    existing_evidence: list[EvidenceItem],
) -> list[EvidenceItem]:
    """Same direct, unranked recent-postmortems listing as before P4, minus
    whichever postmortems are already represented in `existing_evidence` as
    `"runbook"` evidence (P4's goal -- see `_is_duplicate_of_runbook_evidence`
    for the correlation logic). `existing_evidence` is `gather_evidence`'s
    running evidence list at the point this source runs (after source 2,
    `_gather_knowledge_evidence`, per priority order) -- passed in rather
    than re-fetched, so this stays pure evidence-gathering with no new
    retrieval call and no new persistence mechanism, exactly per the P4
    plan's constraints.

    Every kept item now also carries `metadata={"incident_id": ...,
    "postmortem_id": ...}` (previously empty) -- an additive field (see
    `EvidenceItem.metadata`'s own docstring: defaults to `{}`, so this
    doesn't change any pre-existing caller's shape), and doubles as the
    correlation key a *future* runbook evidence item would need to dedupe
    against this one, matching the key `RunbooksConnector.normalize`
    already embeds on the other side.
    """
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

    runbook_evidence = [item for item in existing_evidence if item.source == "runbook"]
    correlated_incident_ids = {
        item.metadata["incident_id"] for item in runbook_evidence if item.metadata.get("incident_id")
    }

    now = datetime.now(UTC)
    result: list[EvidenceItem] = []
    for postmortem in postmortems:
        summary = (postmortem.root_cause or "(no root cause recorded)")[:_EXCERPT_MAX_CHARS]
        if _is_duplicate_of_runbook_evidence(postmortem, summary, runbook_evidence, correlated_incident_ids):
            logger.info(
                "investigation_evidence_postmortem_deduplicated",
                postmortem_id=str(postmortem.id),
                incident_id=str(postmortem.incident_id),
            )
            continue
        result.append(
            EvidenceItem(
                source="postmortem",
                reference=f"postmortem:{postmortem.id}",
                summary=summary,
                retrieved_at=now,
                metadata={"incident_id": str(postmortem.incident_id), "postmortem_id": str(postmortem.id)},
            )
        )
    return result


def _is_duplicate_of_runbook_evidence(
    postmortem: incidents_service.Postmortem,
    summary: str,
    runbook_evidence: list[EvidenceItem],
    correlated_incident_ids: set[str],
) -> bool:
    """True if `postmortem` is already represented among `runbook_evidence`
    -- the same underlying postmortem knowledge, ingested via
    `ingestion.connectors.runbooks` (P1) and retrieved as `"runbook"`
    evidence (P0), should not also appear as a separate `"postmortem"`
    evidence item (P4's goal).

    Prefers the stable correlation key `RunbooksConnector.normalize`
    embeds on every chunk of an ingested postmortem's document --
    `metadata["incident_id"]`, which matches `Postmortem.incident_id`
    one-to-one (a postmortem belongs to exactly one incident, and
    `list_postmortems_for_ingestion` only ever ingests the one current
    approved/published postmortem per incident). This is a clean,
    already-existing key -- not something introduced just for this check
    -- so it is tried first and, whenever present on any runbook evidence
    item, is authoritative: no fallback needed or attempted.

    Falls back to content identity only for runbook evidence items that
    carry no `incident_id` key at all (a manually-proposed/published
    document via P3 legitimately has none -- it was never generated from a
    postmortem) -- this postmortem's own root-cause summary text appearing
    verbatim inside that item's summary. Deliberately strict (full,
    non-empty summary as a substring, not a fuzzy/partial-word match) so an
    unrelated runbook with no correlation key never gets a false-positive
    match against an unrelated postmortem just because both mention common
    words.
    """
    if str(postmortem.incident_id) in correlated_incident_ids:
        return True

    normalized_summary = summary.strip()
    if not normalized_summary or normalized_summary == "(no root cause recorded)":
        return False

    uncorrelated_runbook_evidence = [item for item in runbook_evidence if not item.metadata.get("incident_id")]
    return any(normalized_summary in item.summary for item in uncorrelated_runbook_evidence)


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
    source: Literal["github", "commit", "pull_request", "issue", "slack", "runbook"] | None = None,
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
        retrieved_at=datetime.now(UTC),
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
