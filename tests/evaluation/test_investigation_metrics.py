"""Tests for `app.evaluation.metrics.investigation`."""

from __future__ import annotations

from datetime import UTC, datetime

from app.evaluation.metrics.investigation import (
    evidence_coverage,
    find_unsupported_hypotheses,
    match_expected_hypotheses,
)
from app.evaluation.schemas import ExpectedHypothesis
from app.shared.schemas.agent_contracts import EvidenceItem, RootCauseHypothesis

_NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _evidence(reference: str) -> EvidenceItem:
    return EvidenceItem(source="deployment", reference=reference, summary="...", retrieved_at=_NOW)


def test_evidence_coverage_full_when_all_required_gathered():
    gathered = [_evidence("deploy-456"), _evidence("incident-123")]
    assert evidence_coverage(gathered, ["deploy-456", "incident-123"]) == 1.0


def test_evidence_coverage_partial_when_some_missing():
    gathered = [_evidence("deploy-456")]
    assert evidence_coverage(gathered, ["deploy-456", "incident-123"]) == 0.5


def test_evidence_coverage_none_when_no_requirements():
    assert evidence_coverage([], []) is None


def test_match_expected_hypotheses_passes_on_concept_and_support():
    produced = [
        RootCauseHypothesis(
            description="The connection pool exhaustion caused the outage.",
            confidence=0.8,
            supporting_evidence_ids=["deploy-456"],
        )
    ]
    expected = [
        ExpectedHypothesis(
            concept="connection pool", required_evidence_ids=["deploy-456"], minimum_support=1
        )
    ]
    results = match_expected_hypotheses(produced, expected)
    assert len(results) == 1
    assert results[0].matched
    assert results[0].support_satisfied
    assert results[0].passed


def test_match_expected_hypotheses_fails_when_concept_never_mentioned():
    produced = [
        RootCauseHypothesis(
            description="Unrelated theory.", confidence=0.4, supporting_evidence_ids=["x"]
        )
    ]
    expected = [
        ExpectedHypothesis(concept="connection pool", required_evidence_ids=[], minimum_support=0)
    ]
    results = match_expected_hypotheses(produced, expected)
    assert not results[0].matched
    assert not results[0].passed


def test_match_expected_hypotheses_fails_when_support_below_minimum():
    produced = [
        RootCauseHypothesis(
            description="The connection pool exhaustion caused the outage.",
            confidence=0.8,
            supporting_evidence_ids=["deploy-456"],
        )
    ]
    expected = [
        ExpectedHypothesis(
            concept="connection pool",
            required_evidence_ids=["deploy-456", "incident-123"],
            minimum_support=2,
        )
    ]
    results = match_expected_hypotheses(produced, expected)
    assert results[0].matched
    assert not results[0].support_satisfied
    assert not results[0].passed


def test_find_unsupported_hypotheses_flags_no_real_overlap():
    gathered = [_evidence("deploy-456")]
    produced = [
        RootCauseHypothesis(
            description="theory", confidence=0.5, supporting_evidence_ids=["fabricated"]
        )
    ]
    unsupported = find_unsupported_hypotheses(produced, gathered)
    assert unsupported == produced


def test_find_unsupported_hypotheses_clean_when_real_overlap_exists():
    gathered = [_evidence("deploy-456")]
    produced = [
        RootCauseHypothesis(
            description="theory", confidence=0.5, supporting_evidence_ids=["deploy-456"]
        )
    ]
    unsupported = find_unsupported_hypotheses(produced, gathered)
    assert unsupported == []
