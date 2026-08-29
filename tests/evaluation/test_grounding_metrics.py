"""Tests for `app.evaluation.metrics.grounding`."""

from __future__ import annotations

import uuid

from app.evaluation.metrics.grounding import (
    check_citations,
    check_forbidden_concepts,
    check_required_concepts,
    concepts_traceable_to_evidence,
)
from app.evaluation.schemas import CitationExpectation
from app.shared.schemas.agent_contracts import Citation


def test_check_required_concepts_returns_missing_only():
    text = "The connection pool was exhausted."
    missing = check_required_concepts(text, ["connection pool", "memory leak"])
    assert missing == ["memory leak"]


def test_check_required_concepts_empty_when_all_present():
    missing = check_required_concepts("The connection pool was exhausted.", ["connection pool"])
    assert missing == []


def test_check_forbidden_concepts_flags_violations():
    hits = check_forbidden_concepts("This mentions billing directly.", ["billing", "payroll"])
    assert hits == ["billing"]


def test_check_forbidden_concepts_clean_when_absent():
    hits = check_forbidden_concepts("Nothing sensitive here.", ["billing"])
    assert hits == []


def test_concepts_traceable_to_evidence_true_when_found_in_any_chunk():
    result = concepts_traceable_to_evidence(
        ["connection pool", "kubernetes"], ["The connection pool was exhausted.", "Unrelated text."]
    )
    assert result == {"connection pool": True, "kubernetes": False}


def _citation(chunk_id: uuid.UUID, excerpt: str) -> Citation:
    return Citation(document_id=uuid.uuid4(), chunk_id=chunk_id, source_url=None, excerpt=excerpt)


def test_check_citations_passes_when_resolved_and_supported():
    chunk_id = uuid.uuid4()
    citations = [_citation(chunk_id, "pool was exhausted")]
    result = check_citations(
        citations,
        retrieved_chunk_ids={chunk_id},
        retrieved_chunk_contents={chunk_id: "The connection pool was exhausted after deployment."},
        expectation=CitationExpectation(minimum=1, must_support_answer=True),
    )
    assert result.passed
    assert result.count_satisfied
    assert not result.unresolved_citations
    assert not result.unsupported_citations


def test_check_citations_flags_unresolved_chunk_reference():
    real_chunk_id = uuid.uuid4()
    fabricated_chunk_id = uuid.uuid4()
    citations = [_citation(fabricated_chunk_id, "anything")]
    result = check_citations(
        citations,
        retrieved_chunk_ids={real_chunk_id},
        retrieved_chunk_contents={real_chunk_id: "some content"},
        expectation=CitationExpectation(minimum=1, must_support_answer=True),
    )
    assert not result.passed
    assert str(fabricated_chunk_id) in result.unresolved_citations


def test_check_citations_flags_unsupported_excerpt():
    chunk_id = uuid.uuid4()
    citations = [_citation(chunk_id, "a claim never actually written in the chunk")]
    result = check_citations(
        citations,
        retrieved_chunk_ids={chunk_id},
        retrieved_chunk_contents={chunk_id: "The connection pool was exhausted."},
        expectation=CitationExpectation(minimum=1, must_support_answer=True),
    )
    assert not result.passed
    assert str(chunk_id) in result.unsupported_citations


def test_check_citations_skips_support_check_when_not_required():
    chunk_id = uuid.uuid4()
    citations = [_citation(chunk_id, "a claim never actually written in the chunk")]
    result = check_citations(
        citations,
        retrieved_chunk_ids={chunk_id},
        retrieved_chunk_contents={chunk_id: "The connection pool was exhausted."},
        expectation=CitationExpectation(minimum=1, must_support_answer=False),
    )
    assert result.passed


def test_check_citations_fails_count_when_below_minimum():
    result = check_citations(
        [],
        retrieved_chunk_ids=set(),
        retrieved_chunk_contents={},
        expectation=CitationExpectation(minimum=1, must_support_answer=True),
    )
    assert not result.passed
    assert not result.count_satisfied
