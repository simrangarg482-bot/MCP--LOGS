"""`agents.feedback.patterns` -- totals, the calibration signal, and clustering
of negatively-rated questions. Pure functions with a fake embedder: no model,
no database, no LLM."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.agents.feedback import patterns


def _row(
    query: str | None,
    rating: str = "not_helpful",
    *,
    confidence: float | None = 0.6,
    reason: str | None = None,
    output: dict | None = None,
):
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        input_summary={"query": query} if query is not None else {"incident_id": "x"},
        confidence_score=confidence,
        output_summary=output,
    )
    feedback = SimpleNamespace(rating=rating, reason_code=reason)
    return feedback, execution


async def _embed_by_topic(texts: list[str]) -> list[list[float]]:
    """Two topics, orthogonal: anything mentioning 'vpn' vs everything else."""
    return [[1.0, 0.0] if "vpn" in text.lower() else [0.0, 1.0] for text in texts]


def test_totals_count_ratings_and_group_reasons() -> None:
    rows = [
        _row("a", "helpful"),
        _row("b", reason="incorrect"),
        _row("c", reason="incorrect"),
        _row("d"),
    ]

    totals = patterns.summarize_totals(rows)

    assert (totals.total, totals.helpful, totals.not_helpful) == (4, 1, 3)
    assert totals.not_helpful_by_reason == {"incorrect": 2, "unspecified": 1}


def test_calibration_buckets_split_by_confidence_and_skip_unknown() -> None:
    rows = [
        _row("a", "helpful", confidence=0.95),
        _row("b", "not_helpful", confidence=0.9),  # confidently wrong-ish
        _row("c", "not_helpful", confidence=0.3),
        _row("d", "helpful", confidence=None),  # skipped, not counted as 0
    ]

    buckets = patterns.summarize_calibration(rows)

    top = buckets[-1]
    assert (top.range_low, top.range_high, top.count) == (0.85, 1.0, 2)
    assert top.helpful_rate == pytest.approx(0.5)
    assert buckets[0].count == 1 and buckets[0].helpful_rate == 0.0
    assert sum(bucket.count for bucket in buckets) == 3
    assert buckets[1].helpful_rate is None  # empty band has no rate, not 0


@pytest.mark.asyncio
async def test_similar_negative_questions_form_one_unreviewed_pattern() -> None:
    rows = [
        _row("How do I reset my VPN token?", confidence=0.5, reason="outdated",
             output={"answer_mode": "no_answer", "citation_count": 0}),
        _row("vpn token reset steps", confidence=0.7, reason="outdated",
             output={"answer_mode": "answered", "citation_count": 2}),
        _row("Unrelated billing question"),  # a singleton: not recurring
    ]

    found = await patterns.identify_patterns(rows, embed=_embed_by_topic, similarity_threshold=0.9)

    assert len(found) == 1
    pattern = found[0]
    assert pattern.not_helpful_count == 2
    assert pattern.kind == "automated_pattern" and pattern.reviewed is False
    assert pattern.avg_confidence == pytest.approx(0.6)
    assert pattern.no_evidence_share == pytest.approx(0.5)
    assert pattern.top_reason_codes == ["outdated"]
    assert len(pattern.execution_ids) == 2


@pytest.mark.asyncio
async def test_helpful_ratings_never_contribute_to_a_pattern() -> None:
    rows = [_row("vpn one", "helpful"), _row("vpn two", "helpful"), _row("vpn three")]

    assert await patterns.identify_patterns(rows, embed=_embed_by_topic, similarity_threshold=0.9) == []


@pytest.mark.asyncio
async def test_executions_without_question_text_are_ignored() -> None:
    rows = [_row(None), _row(None)]

    assert await patterns.identify_patterns(rows, embed=_embed_by_topic, similarity_threshold=0.9) == []


@pytest.mark.asyncio
async def test_pattern_text_is_redacted_and_truncated() -> None:
    secret = "ghp_" + "d" * 36
    long_tail = "x" * 500
    rows = [_row(f"vpn broken {secret} {long_tail}"), _row("vpn also broken")]

    (pattern,) = await patterns.identify_patterns(
        rows, embed=_embed_by_topic, similarity_threshold=0.9
    )

    assert secret not in pattern.representative_query
    assert len(pattern.representative_query) <= patterns.MAX_QUERY_CHARS


@pytest.mark.asyncio
async def test_largest_pattern_is_listed_first() -> None:
    rows = [_row("billing a"), _row("billing b")] + [_row(f"vpn {i}") for i in range(3)]

    found = await patterns.identify_patterns(rows, embed=_embed_by_topic, similarity_threshold=0.9)

    assert [pattern.not_helpful_count for pattern in found] == [3, 2]
