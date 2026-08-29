"""Tests for `app.evaluation.metrics.retrieval` -- pure functions, no
fixtures/adapters involved."""

from __future__ import annotations

from app.evaluation.metrics.retrieval import (
    mean_of,
    mean_reciprocal_rank,
    precision_at_k,
    recall_at_k,
    relevant_document_coverage,
)


def test_recall_at_k_finds_all_relevant_within_k():
    assert recall_at_k(["a", "b", "c"], {"a", "c"}, k=3) == 1.0


def test_recall_at_k_partial_when_relevant_doc_outside_k():
    assert recall_at_k(["a", "b", "c", "d"], {"a", "d"}, k=2) == 0.5


def test_recall_at_k_none_when_no_relevant_ids_declared():
    assert recall_at_k(["a", "b"], set(), k=5) is None


def test_precision_at_k_counts_relevant_among_top_k():
    assert precision_at_k(["a", "b", "c"], {"a", "c"}, k=3) == 2 / 3


def test_precision_at_k_none_when_nothing_retrieved():
    assert precision_at_k([], {"a"}, k=5) is None


def test_precision_at_k_zero_when_top_k_has_no_relevant_docs():
    assert precision_at_k(["x", "y"], {"a"}, k=2) == 0.0


def test_mrr_reciprocal_of_first_relevant_rank():
    assert mean_reciprocal_rank(["x", "a", "y"], {"a"}) == 0.5


def test_mrr_one_when_first_result_is_relevant():
    assert mean_reciprocal_rank(["a", "x"], {"a"}) == 1.0


def test_mrr_zero_when_no_relevant_doc_present():
    assert mean_reciprocal_rank(["x", "y"], {"a"}) == 0.0


def test_mrr_none_when_no_relevant_ids_declared():
    assert mean_reciprocal_rank(["x"], set()) is None


def test_relevant_document_coverage_ignores_rank():
    # "d" is relevant but far down the list -- coverage is unbounded by k,
    # unlike recall_at_k with a small k.
    assert relevant_document_coverage(["a", "b", "c", "d"], {"a", "d"}) == 1.0


def test_relevant_document_coverage_none_when_no_relevant_ids():
    assert relevant_document_coverage(["a"], set()) is None


def test_multiple_relevant_documents_partial_coverage():
    assert relevant_document_coverage(["a"], {"a", "b", "c"}) == 1 / 3


def test_mean_of_skips_none_values():
    assert mean_of([1.0, None, 3.0]) == 2.0


def test_mean_of_all_none_returns_none():
    assert mean_of([None, None]) is None


def test_recall_at_k_rejects_non_positive_k():
    import pytest

    with pytest.raises(ValueError):
        recall_at_k(["a"], {"a"}, k=0)
