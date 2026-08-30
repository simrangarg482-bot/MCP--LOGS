import pytest

from app.agents.confidence import _normalize_rerank_score


def test_reranker_calibration_maps_observed_borderline_logit_to_half() -> None:
    assert _normalize_rerank_score(-8.0) == pytest.approx(0.5)


def test_reranker_calibration_separates_strong_and_weak_live_scores() -> None:
    assert _normalize_rerank_score(-3.0) > 0.9
    assert _normalize_rerank_score(-12.0) < 0.15


def test_reranker_calibration_is_numerically_stable() -> None:
    assert _normalize_rerank_score(10_000.0) == pytest.approx(1.0)
    assert _normalize_rerank_score(-10_000.0) == pytest.approx(0.0)
