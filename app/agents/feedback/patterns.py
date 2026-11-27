"""Aggregate, read-only analysis of raw feedback: totals, confidence
calibration signal, and clusters of negatively-rated questions.

Nothing here persists, calls an LLM, or changes any agent behaviour. It reuses
the Knowledge Gap Agent's local-embedding leader clustering
(`agents.knowledge_gap.clustering`) -- the same cheap, deterministic grouping,
pointed at a different question: not "what do people ask that we can't answer
confidently" but "what do people ask whose answers they were unhappy with".

WHAT A PATTERN MEANS (and does not). A pattern is several users rating
answers to similar questions "not helpful". That is a *lead* for a curator.
It is not evidence the answers were wrong: the corpus may genuinely lack the
answer, the user may have wanted something else, or the rating may be a
misclick. The output type says so (`reviewed=False`, `kind="automated_pattern"`).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable, Sequence

from app.agents.feedback.redaction import redact_secrets
from app.agents.feedback.schemas import (
    CalibrationBucketSummary,
    FeedbackPattern,
    RatingTotals,
)
from app.agents.knowledge_gap.clustering import cluster_by_similarity
from app.database.models.agent_models import AgentExecution, AnswerFeedback

Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]

#: A "recurring" pattern needs at least this many not-helpful executions.
MIN_PATTERN_SIZE = 2
#: Bounds the text a curator sees per pattern (and the size of any export).
MAX_QUERY_CHARS = 200
#: Upper edges of the confidence bands used for the calibration signal.
_CONFIDENCE_EDGES: tuple[float, ...] = (0.5, 0.7, 0.85, 1.0)

FeedbackRow = tuple[AnswerFeedback, AgentExecution]


def safe_query_text(execution: AgentExecution) -> str | None:
    """The execution's question, redacted and truncated -- or None when the
    execution has no free-text question (a `triage_incident` run records only
    an incident id)."""
    summary = execution.input_summary or {}
    query = summary.get("query")
    if not isinstance(query, str) or not query.strip():
        return None
    redacted, _ = redact_secrets(query.strip())
    return redacted[:MAX_QUERY_CHARS]


def summarize_totals(rows: Sequence[FeedbackRow]) -> RatingTotals:
    helpful = sum(1 for fb, _ in rows if fb.rating == "helpful")
    not_helpful = sum(1 for fb, _ in rows if fb.rating == "not_helpful")
    reasons = Counter(
        (fb.reason_code or "unspecified") for fb, _ in rows if fb.rating == "not_helpful"
    )
    return RatingTotals(
        total=len(rows),
        helpful=helpful,
        not_helpful=not_helpful,
        not_helpful_by_reason=dict(reasons),
    )


def summarize_calibration(rows: Sequence[FeedbackRow]) -> list[CalibrationBucketSummary]:
    """Helpful rate per confidence band. Executions with no recorded
    confidence are skipped, not bucketed as zero."""
    buckets: list[list[int]] = [[0, 0] for _ in _CONFIDENCE_EDGES]  # [helpful, not_helpful]
    for fb, execution in rows:
        confidence = execution.confidence_score
        if confidence is None:
            continue
        for position, upper in enumerate(_CONFIDENCE_EDGES):
            if confidence < upper or position == len(_CONFIDENCE_EDGES) - 1:
                buckets[position][0 if fb.rating == "helpful" else 1] += 1
                break

    summaries: list[CalibrationBucketSummary] = []
    lower = 0.0
    for upper, (helpful, not_helpful) in zip(_CONFIDENCE_EDGES, buckets, strict=True):
        count = helpful + not_helpful
        summaries.append(
            CalibrationBucketSummary(
                range_low=lower,
                range_high=upper,
                count=count,
                helpful=helpful,
                not_helpful=not_helpful,
                helpful_rate=(helpful / count) if count else None,
            )
        )
        lower = upper
    return summaries


def _has_no_evidence(execution: AgentExecution) -> bool | None:
    """True when the run answered with no citations / declined; None when the
    run recorded no output summary (older runs)."""
    output = execution.output_summary
    if not output:
        return None
    return output.get("answer_mode") == "no_answer" or output.get("citation_count", 0) == 0


async def identify_patterns(
    rows: Sequence[FeedbackRow],
    *,
    embed: Embedder,
    similarity_threshold: float,
    min_size: int = MIN_PATTERN_SIZE,
) -> list[FeedbackPattern]:
    """Cluster the not-helpful rows by question similarity, largest first."""
    candidates: list[tuple[AnswerFeedback, AgentExecution, str]] = []
    for fb, execution in rows:
        if fb.rating != "not_helpful":
            continue
        text = safe_query_text(execution)
        if text:
            candidates.append((fb, execution, text))
    if len(candidates) < min_size:
        return []

    embeddings = await embed([text for _, _, text in candidates])
    clusters = cluster_by_similarity(embeddings, similarity_threshold=similarity_threshold)

    patterns: list[FeedbackPattern] = []
    for member_indices in clusters:
        if len(member_indices) < min_size:
            continue
        members = [candidates[i] for i in member_indices]
        confidences = [
            ex.confidence_score for _, ex, _ in members if ex.confidence_score is not None
        ]
        evidence_flags = [
            flag for flag in (_has_no_evidence(ex) for _, ex, _ in members) if flag is not None
        ]
        reasons = Counter(fb.reason_code for fb, _, _ in members if fb.reason_code)
        patterns.append(
            FeedbackPattern(
                representative_query=members[0][2],
                execution_ids=[ex.id for _, ex, _ in members],
                not_helpful_count=len(members),
                avg_confidence=(sum(confidences) / len(confidences)) if confidences else None,
                no_evidence_share=(
                    sum(1 for flag in evidence_flags if flag) / len(evidence_flags)
                    if evidence_flags
                    else None
                ),
                top_reason_codes=[code for code, _ in reasons.most_common(3)],
            )
        )
    patterns.sort(key=lambda pattern: pattern.not_helpful_count, reverse=True)
    return patterns
