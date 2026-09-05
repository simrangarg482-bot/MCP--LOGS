"""The Confidence Evaluation Node (AGENT_WORKFLOWS.md section 2.2 /
PROJECT_PLAN.md section 6.2): deterministic, no-LLM scoring that combines
retrieval signals into `confidence_score` and decides `route`.

Owned by: agents/. Unlike every other node in this graph, this one is pure
computation -- no LLM call, no database/vector-store/network I/O -- so it
has no retryable-failure path (`agents.retry` is not used here) and cannot
time out or fail transiently. AGENT_WORKFLOWS.md section 2.2's own framing:
"the only 'failure' is an eventually-tuned threshold being wrong, which is a
data/tuning problem, not a code-failure one."

Weights and the exact combination formula are a placeholder
(`ENGINEERING_DECISIONS.md`'s "Open" section: "Confidence-score formula and
threshold -- will be decided empirically once real retrieval data exists"),
not a tuned model. The load-bearing property here is architectural, not
numerical: this function is pure and unit-testable with synthetic
`confidence_signals` inputs, exactly as AGENT_WORKFLOWS.md section 2.2
requires.

**Signal sourcing, since not every signal is computed here:**
- `top_similarity` -- seeded into `state.confidence_signals` by the
  Retrieval Agent (`agents.retrieval.node`) via
  `retrieval.service.search_with_signals`, *before* reranking overwrites
  each chunk's `.score`. It is the top *dense* hit's cosine similarity
  (inner product of L2-normalized vectors), a real semantic-match
  magnitude -- **not** the fused RRF score it used to be. That earlier
  choice pinned this signal at a near-constant ~0.5 for virtually every
  query (the top fused chunk is rarely rank-1 in both the dense and lexical
  list, and is mechanically exactly 0.5 whenever the lexical list is
  empty), so it discriminated nothing and merely added a constant offset to
  every confidence score (EKIP audit 2026-09-02, finding 2). Normalized to
  0-1 here between an empirical floor and ceiling (see
  `_normalize_top_similarity`).
- `rerank_score` -- computed here from `state.retrieved_chunks[0].score`
  (the cross-encoder score `agents.retrieval.reranking` already wrote onto
  each chunk). Cross-encoder scores are unbounded logits, squashed through
  a sigmoid to land on the same 0-1 scale as the other signals.
- `source_count` -- computed here: number of *distinct documents* (not
  chunks) in `state.retrieved_chunks`, mapped through a diminishing-returns
  curve (see `_distinct_source_count_signal`) so a single authoritative
  document already scores well. The earlier linear `n / 5` mapping scored a
  fully-correct single-document answer at 0.2, structurally penalising the
  common case where one runbook/README/postmortem is the complete and
  correct source (EKIP audit 2026-09-02, finding 1).
- `historical_similarity` -- for incident-triage calls only, per
  AGENT_WORKFLOWS.md. Seeded into `state.confidence_signals` by the
  Retrieval Agent (`agents.retrieval.node`) exactly like `top_similarity`,
  but from a second, `collection="incidents"`-scoped
  `retrieval.service.search_with_signals` call (only issued when
  `state.incident_id is not None`) against the real "incidents" retrieval
  collection (`app.ingestion.connectors.incidents.IncidentsConnector`
  re-ingests closed/resolved incidents into it -- see
  `app.database.models.retrieval_models.IncidentChunk`). It is the same
  kind of value as `top_similarity` -- a raw dense cosine similarity from
  the same all-MiniLM-L6-v2 embedding space -- so it is normalized the same
  way, via `_normalize_top_similarity`, below. When no historical incident
  matches (or that search fails), the Retrieval Agent simply omits the key
  rather than fabricating 0.0, and `_weighted_score` renormalizes over
  whichever signals are actually present, so its absence on a given call
  doesn't silently deflate that call's score relative to one where a match
  was found.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from app.agents.graph import GraphState
from app.retrieval.schemas import ScoredChunk
from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings

logger = get_logger(__name__)

# Placeholder relative weights (see module docstring) -- not independently
# meaningful magnitudes, and renormalized over whichever signals are
# actually present for a given call (`_weighted_score`).
_SIGNAL_WEIGHTS: dict[str, float] = {
    "top_similarity": 0.40,
    "rerank_score": 0.35,
    "source_count": 0.15,
    "historical_similarity": 0.10,
}

# `_distinct_source_count_signal`'s per-source multiplier: with one distinct
# document the signal is `1 - _SOURCE_COUNT_DECAY` (0.70), with two it is
# `1 - _SOURCE_COUNT_DECAY**2` (0.91), and so on -- see AGENT_WORKFLOWS.md
# section 2.2 ("five chunks from one stale doc is weaker evidence than one
# chunk each from five sources"): additional *distinct* sources still add
# confidence, but with diminishing marginal value, and a single solid source
# is no longer treated as near-zero evidence.
_SOURCE_COUNT_DECAY = 0.30

# `_normalize_top_similarity`'s min-max endpoints for all-MiniLM-L6-v2
# query/document cosine similarity. `_DENSE_SIMILARITY_FLOOR` is the same
# "genuine topical match" cut-off `app.shared.config.settings.
# memory_relevance_threshold` already uses for this exact embedding model;
# `_DENSE_SIMILARITY_CEILING` is a deliberately conservative "strong match"
# value. Both are provisional placeholders in the same sense
# `_normalize_rerank_score`'s were before the 2026-08-30 live trace
# calibrated them -- re-run `scripts/eval_confidence.py` against live data
# and tighten these (and re-tune `Settings.confidence_threshold`, whose
# whole score distribution this change shifts) before trusting the routing
# numbers. See that script and the `confidence_threshold` comment in
# `app/shared/config/settings.py`.
_DENSE_SIMILARITY_FLOOR = 0.35
_DENSE_SIMILARITY_CEILING = 0.65


def evaluate_confidence(state: GraphState) -> dict[str, Any]:
    """Pure function: `GraphState` in, a partial-state update out. The
    LangGraph-callable node (`confidence_evaluation_node` below) is a
    trivial synchronous wrapper around this, kept separate so the scoring
    logic itself stays unit-testable with a bare `GraphState` and no
    LangGraph machinery involved.
    """
    chunks = state.retrieved_chunks

    # Carries "top_similarity", seeded by the Retrieval Agent -- see module
    # docstring.
    signals = dict(state.confidence_signals)
    if "top_similarity" in signals:
        signals["top_similarity"] = _normalize_top_similarity(signals["top_similarity"])

    signals["rerank_score"] = _normalize_rerank_score(chunks[0].score) if chunks else 0.0
    signals["source_count"] = _distinct_source_count_signal(chunks)

    if state.incident_id is None:
        # historical_similarity only applies to incident-triage calls
        # (AGENT_WORKFLOWS.md section 2.2) -- drop it if some future caller
        # ever seeds it for a non-triage query.
        signals.pop("historical_similarity", None)
    elif "historical_similarity" in signals:
        # Same embedding model/scale as `top_similarity` (both are
        # all-MiniLM-L6-v2 dense cosine similarities) -- see module
        # docstring -- so the same floor/ceiling normalization applies.
        # Absent entirely (not normalized to 0.0) when the Retrieval Agent
        # found no historical match; see module docstring.
        signals["historical_similarity"] = _normalize_top_similarity(
            signals["historical_similarity"]
        )

    confidence_score = _weighted_score(signals)
    # `confidence_threshold`'s default (0.5, provisional since the
    # 2026-09-02 signal changes) has a documented basis -- see
    # `scripts/eval_confidence.py` and the evidence comment on this field in
    # `app/shared/config/settings.py` before changing it.
    threshold = get_settings().confidence_threshold
    route: Literal["answer", "investigation"] = (
        "answer" if confidence_score >= threshold else "investigation"
    )

    logger.info(
        "confidence_evaluated",
        confidence_score=confidence_score,
        threshold=threshold,
        route=route,
        signals=signals,
    )

    return {
        "confidence_score": confidence_score,
        "confidence_signals": signals,
        "route": route,
    }


def confidence_evaluation_node(state: GraphState) -> dict[str, Any]:
    """The LangGraph-callable node. Synchronous (not `async def`, unlike
    every other node in this graph) -- an honest reflection of this node
    doing no I/O, per its own documented "no retryable failures" property.
    """
    return evaluate_confidence(state)


def _normalize_top_similarity(raw_dense_similarity: float) -> float:
    """Min-max normalize the top dense hit's cosine similarity to 0-1
    between `_DENSE_SIMILARITY_FLOOR` and `_DENSE_SIMILARITY_CEILING`,
    clamped at both ends.

    Fed a real semantic-match magnitude by `agents.retrieval.node` (via
    `retrieval.service.search_with_signals`) -- the inner product of the
    L2-normalized query and best-matching chunk vectors. It was previously
    fed the top candidate's *fused RRF score* and divided by a theoretical
    2-list-agreement ceiling, which made it a near-constant ~0.5 for
    essentially every query and so contributed nothing but a fixed offset
    to the weighted score (EKIP audit 2026-09-02, finding 2).
    """
    span = _DENSE_SIMILARITY_CEILING - _DENSE_SIMILARITY_FLOOR
    return max(0.0, min(1.0, (raw_dense_similarity - _DENSE_SIMILARITY_FLOOR) / span))


def _normalize_rerank_score(raw_score: float) -> float:
    """Calibrate the fixed MS-MARCO cross-encoder logit onto 0-1.

    A plain ``sigmoid(raw_score)`` was badly miscalibrated for this model:
    live, answer-bearing results commonly score between -8 and -3, so it
    mapped useful evidence to effectively zero and routed every benchmark
    question to Investigation.  The center (-8) and temperature (2) are
    based on the 2026-08-30 controlled live trace: -8 is borderline and -3
    is strong. Sufficiency and grounding still fail closed after this gate;
    this calibration only lets plausible evidence reach those checks.
    """
    calibrated_logit = (raw_score + 8.0) / 2.0
    if calibrated_logit >= 0:
        return 1.0 / (1.0 + math.exp(-calibrated_logit))
    exp_value = math.exp(calibrated_logit)
    return exp_value / (1.0 + exp_value)


def _distinct_source_count_signal(chunks: list[ScoredChunk]) -> float:
    """Number of *distinct documents* represented in `chunks` (not chunk
    count -- AGENT_WORKFLOWS.md section 2.2's own distinction), mapped to
    0-1 through a diminishing-returns curve: 0 sources -> 0.0, 1 -> 0.70,
    2 -> 0.91, 3 -> 0.973, 4 -> 0.992.

    A single authoritative document (one runbook, one README, one
    postmortem) is often the complete and correct source for a question,
    not weak evidence -- the earlier linear `distinct_count / 5` mapping
    scored that common case at 0.2 and dragged otherwise-answerable
    single-source questions below the routing threshold (EKIP audit
    2026-09-02, finding 1). Additional distinct sources still raise the
    signal (corroboration across independent documents is genuinely
    stronger evidence), just with diminishing marginal value rather than
    being the precondition for any confidence at all.
    """
    distinct_document_count = len({chunk.document_id for chunk in chunks})
    if distinct_document_count == 0:
        return 0.0
    return 1.0 - _SOURCE_COUNT_DECAY**distinct_document_count


def _weighted_score(signals: dict[str, float]) -> float:
    """Weighted average over whichever signals are present in `signals`,
    renormalizing `_SIGNAL_WEIGHTS` to sum to 1 over just those keys -- see
    module docstring on why `historical_similarity`'s frequent absence must
    not silently deflate every score computed without it.
    """
    applicable_weights = {
        key: weight for key, weight in _SIGNAL_WEIGHTS.items() if key in signals
    }
    if not applicable_weights:
        return 0.0
    total_weight = sum(applicable_weights.values())
    return (
        sum(signals[key] * weight for key, weight in applicable_weights.items()) / total_weight
    )
