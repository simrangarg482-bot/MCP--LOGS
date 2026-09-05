"""Unit coverage for `app.agents.confidence` -- the deterministic,
no-LLM routing formula.

The module docstring and `AGENT_WORKFLOWS.md` section 2.2 both call this
function out as "pure and unit-testable with synthetic `confidence_signals`
inputs"; until this file it had no such test (only `_normalize_rerank_score`
was covered, in `test_confidence_calibration.py`). These tests pin the
routing decision, the weighted-average math, the triage-only handling of
`historical_similarity`, and -- explicitly -- the two signal-quality
regressions fixed after the 2026-09-02 audit:

  1. a single-source answer must not be scored as near-zero evidence
     (`_distinct_source_count_signal`);
  2. `top_similarity` must actually discriminate, not sit pinned at ~0.5
     for every query (`_normalize_top_similarity`).
"""

from __future__ import annotations

import uuid

import pytest

from app.agents import confidence as confidence_module
from app.agents.confidence import (
    _DENSE_SIMILARITY_CEILING,
    _DENSE_SIMILARITY_FLOOR,
    _distinct_source_count_signal,
    _normalize_top_similarity,
    _weighted_score,
    evaluate_confidence,
)
from app.agents.graph import GraphState
from app.retrieval.schemas import ScoredChunk
from app.shared.schemas import Identity

_ACTOR = Identity.for_agent("test_confidence", uuid.uuid4())


class _FakeSettings:
    """Stand-in for `get_settings()` so a repo-local `.env` cannot move the
    threshold out from under these tests.
    """

    def __init__(self, threshold: float) -> None:
        self.confidence_threshold = threshold


def _chunk(document_id: uuid.UUID, *, score: float = 0.9) -> ScoredChunk:
    return ScoredChunk(
        chunk_id=uuid.uuid4(),
        document_id=document_id,
        collection="documentation",
        content="some retrieved content",
        score=score,
        source_offset_start=0,
        source_offset_end=21,
    )


def _state(
    *,
    chunks: list[ScoredChunk] | None = None,
    signals: dict[str, float] | None = None,
    incident_id: uuid.UUID | None = None,
) -> GraphState:
    return GraphState(
        query="why did checkout break?",
        actor=_ACTOR,
        incident_id=incident_id,
        retrieved_chunks=chunks or [],
        confidence_signals=signals or {},
    )


# --------------------------------------------------------------------------
# _distinct_source_count_signal -- audit finding 1
# --------------------------------------------------------------------------


def test_source_count_zero_when_no_chunks() -> None:
    assert _distinct_source_count_signal([]) == 0.0


def test_single_source_is_strong_not_near_zero() -> None:
    """Audit finding 1: the old linear `n / 5` mapping scored a
    fully-correct single-document answer at 0.2. One authoritative document
    is often the complete answer -- it must clear most of this signal.
    """
    one_doc = uuid.uuid4()
    signal = _distinct_source_count_signal([_chunk(one_doc), _chunk(one_doc), _chunk(one_doc)])

    assert signal == pytest.approx(0.70)
    assert signal > 0.5  # the regression guard: never back to ~0.2


def test_source_count_has_diminishing_returns_and_is_monotonic() -> None:
    signals = [
        _distinct_source_count_signal([_chunk(uuid.uuid4()) for _ in range(n)])
        for n in range(1, 6)
    ]

    assert signals == sorted(signals)  # more distinct sources never hurts
    assert signals[0] == pytest.approx(0.70)
    assert signals[1] == pytest.approx(0.91)
    assert signals[-1] < 1.0  # asymptotic, never actually saturates
    # marginal gains shrink: 1->2 adds more than 3->4
    assert (signals[1] - signals[0]) > (signals[3] - signals[2])


def test_source_count_counts_distinct_documents_not_chunks() -> None:
    doc = uuid.uuid4()
    five_chunks_one_doc = [_chunk(doc) for _ in range(5)]

    assert _distinct_source_count_signal(five_chunks_one_doc) == pytest.approx(0.70)


# --------------------------------------------------------------------------
# _normalize_top_similarity -- audit finding 2
# --------------------------------------------------------------------------


def test_top_similarity_clamps_below_floor_and_above_ceiling() -> None:
    assert _normalize_top_similarity(_DENSE_SIMILARITY_FLOOR - 0.2) == 0.0
    assert _normalize_top_similarity(0.0) == 0.0
    assert _normalize_top_similarity(_DENSE_SIMILARITY_CEILING + 0.3) == 1.0
    assert _normalize_top_similarity(1.0) == 1.0


def test_top_similarity_is_monotonic_between_floor_and_ceiling() -> None:
    midpoint = (_DENSE_SIMILARITY_FLOOR + _DENSE_SIMILARITY_CEILING) / 2

    low = _normalize_top_similarity(_DENSE_SIMILARITY_FLOOR + 0.01)
    mid = _normalize_top_similarity(midpoint)
    high = _normalize_top_similarity(_DENSE_SIMILARITY_CEILING - 0.01)

    assert 0.0 < low < mid < high < 1.0
    assert mid == pytest.approx(0.5)


def test_top_similarity_actually_discriminates_strong_from_weak() -> None:
    """Audit finding 2: previously this signal was fed the fused RRF score
    and sat pinned at ~0.5 for essentially every query (strong match and
    out-of-domain junk alike). A real cosine similarity must separate them.
    """
    strong_match = _normalize_top_similarity(0.62)
    weak_match = _normalize_top_similarity(0.30)

    assert strong_match > 0.8
    assert weak_match == 0.0
    assert strong_match - weak_match > 0.5


# --------------------------------------------------------------------------
# evaluate_confidence -- the routing decision
# --------------------------------------------------------------------------


def test_high_signals_route_to_answer(monkeypatch) -> None:
    monkeypatch.setattr(confidence_module, "get_settings", lambda: _FakeSettings(0.6))
    docs = [_chunk(uuid.uuid4()) for _ in range(4)]

    out = evaluate_confidence(_state(chunks=docs, signals={"top_similarity": 0.62}))

    assert out["route"] == "answer"
    assert out["confidence_score"] >= 0.6


def test_out_of_domain_signals_route_to_investigation(monkeypatch) -> None:
    monkeypatch.setattr(confidence_module, "get_settings", lambda: _FakeSettings(0.6))
    # top dense similarity below the floor, cross-encoder rejects the top
    # chunk (very negative logit) -- the no-information shape.
    docs = [_chunk(uuid.uuid4(), score=-20.0) for _ in range(6)]

    out = evaluate_confidence(_state(chunks=docs, signals={"top_similarity": 0.18}))

    assert out["route"] == "investigation"
    assert out["confidence_score"] < 0.6


def test_route_is_decided_strictly_at_the_threshold(monkeypatch) -> None:
    """`route == "answer"` iff `confidence_score >= threshold` -- the exact
    comparison `scripts/eval_confidence.py` replays for its sweep.
    """
    docs = [_chunk(uuid.uuid4()) for _ in range(3)]
    state = _state(chunks=docs, signals={"top_similarity": 0.5})
    score = evaluate_confidence(state)["confidence_score"]

    monkeypatch.setattr(confidence_module, "get_settings", lambda: _FakeSettings(score))
    assert evaluate_confidence(state)["route"] == "answer"  # >= is inclusive

    monkeypatch.setattr(
        confidence_module, "get_settings", lambda: _FakeSettings(score + 1e-6)
    )
    assert evaluate_confidence(state)["route"] == "investigation"


def test_empty_retrieval_scores_effectively_zero_and_investigates(monkeypatch) -> None:
    monkeypatch.setattr(confidence_module, "get_settings", lambda: _FakeSettings(0.6))

    out = evaluate_confidence(_state(chunks=[], signals={"top_similarity": 0.0}))

    assert out["confidence_score"] == pytest.approx(0.0)
    assert out["route"] == "investigation"


def test_historical_similarity_dropped_for_non_triage_calls(monkeypatch) -> None:
    monkeypatch.setattr(confidence_module, "get_settings", lambda: _FakeSettings(0.6))
    docs = [_chunk(uuid.uuid4())]

    out = evaluate_confidence(
        _state(
            chunks=docs,
            signals={"top_similarity": 0.5, "historical_similarity": 0.9},
            incident_id=None,
        )
    )

    assert "historical_similarity" not in out["confidence_signals"]


def test_historical_similarity_kept_for_triage_calls(monkeypatch) -> None:
    """`historical_similarity` is now a real signal (audit finding 6): the
    Retrieval Agent seeds it from a `collection="incidents"` search's raw
    dense cosine similarity, and -- exactly like `top_similarity`, the same
    embedding-model scale -- `evaluate_confidence` normalizes it via
    `_normalize_top_similarity` rather than passing the raw value through
    unchanged.
    """
    monkeypatch.setattr(confidence_module, "get_settings", lambda: _FakeSettings(0.6))
    docs = [_chunk(uuid.uuid4())]

    out = evaluate_confidence(
        _state(
            chunks=docs,
            signals={"top_similarity": 0.5, "historical_similarity": 0.9},
            incident_id=uuid.uuid4(),
        )
    )

    assert out["confidence_signals"]["historical_similarity"] == pytest.approx(
        _normalize_top_similarity(0.9)
    )


def test_historical_similarity_absent_for_triage_call_with_no_historical_match(
    monkeypatch,
) -> None:
    """Safe-when-absent case (audit finding 6, requirement 9's last two
    bullets): a triage call whose incidents search found nothing historical
    simply never has the key in `state.confidence_signals` to begin with
    (the Retrieval Agent omits it rather than seeding a fabricated 0.0) --
    `evaluate_confidence` must not synthesize it, and the weighted score
    must renormalize over the signals that remain.
    """
    monkeypatch.setattr(confidence_module, "get_settings", lambda: _FakeSettings(0.6))
    docs = [_chunk(uuid.uuid4())]

    out = evaluate_confidence(
        _state(
            chunks=docs,
            signals={"top_similarity": 0.5},
            incident_id=uuid.uuid4(),
        )
    )

    assert "historical_similarity" not in out["confidence_signals"]
    assert 0.0 <= out["confidence_score"] <= 1.0


# --------------------------------------------------------------------------
# _weighted_score -- renormalization over present signals
# --------------------------------------------------------------------------


def test_weighted_score_renormalizes_over_present_signals_only() -> None:
    # Only two of the four signals present -- weights 0.40 and 0.35 must be
    # renormalized to sum to 1, not treated as if the missing signals were 0.
    score = _weighted_score({"top_similarity": 1.0, "rerank_score": 0.0})

    assert score == pytest.approx(0.40 / (0.40 + 0.35))


def test_weighted_score_is_zero_with_no_signals() -> None:
    assert _weighted_score({}) == 0.0
