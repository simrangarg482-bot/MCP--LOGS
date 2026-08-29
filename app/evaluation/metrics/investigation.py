"""Initial Investigation Agent evaluation: evidence coverage, hypothesis
support, and unsupported-hypothesis detection.

Operates directly on the real agent output types
(`app.shared.schemas.agent_contracts.EvidenceItem`/`RootCauseHypothesis`) --
not a parallel evaluation-only representation of them -- per this package's
"the evaluator measures the system, it does not become a second
implementation of it" rule. `EvidenceItem.reference` is the join key between
gathered evidence and a hypothesis's `supporting_evidence_ids`, exactly as
`app.agents.investigation.hypothesis` itself uses it (per
`RootCauseHypothesis`'s own docstring: "must reference real
`EvidenceItem.reference` values from the same investigation").

Deliberately small for this first version, per this package's spec ("do not
over-engineer this first version"): no contradiction-detection beyond
"claims support that doesn't exist," no cross-hypothesis consistency
checking.
"""

from __future__ import annotations

from app.evaluation.metrics.retrieval import relevant_document_coverage
from app.evaluation.schemas import ExpectedHypothesis, normalize_text
from app.shared.schemas.agent_contracts import EvidenceItem, RootCauseHypothesis


def evidence_coverage(
    gathered_evidence: list[EvidenceItem], required_evidence_ids: list[str]
) -> float | None:
    """Fraction of `required_evidence_ids` present among `gathered_evidence`'s
    `.reference` values -- same "was every important item retrieved at all"
    question `relevant_document_coverage` answers for retrieval, reused
    directly rather than re-implemented (the two are the same set-membership
    computation over different id vocabularies).
    """
    references = [item.reference for item in gathered_evidence]
    return relevant_document_coverage(references, set(required_evidence_ids))


class HypothesisMatchResult:
    """Outcome of matching one `ExpectedHypothesis` against the hypotheses
    the agent actually produced."""

    def __init__(
        self,
        expected: ExpectedHypothesis,
        *,
        matched: bool,
        matched_hypothesis: RootCauseHypothesis | None,
        support_satisfied: bool,
    ) -> None:
        self.expected = expected
        self.matched = matched
        self.matched_hypothesis = matched_hypothesis
        self.support_satisfied = support_satisfied

    @property
    def passed(self) -> bool:
        return self.matched and self.support_satisfied


def match_expected_hypotheses(
    produced: list[RootCauseHypothesis], expected: list[ExpectedHypothesis]
) -> list[HypothesisMatchResult]:
    """For each `expected` hypothesis, find a produced hypothesis whose
    `description` mentions the expected `concept` (normalized substring
    match -- descriptions are free LLM text, so this is deliberately loose,
    the same tolerance `tests/rag_validation`'s `expected_fact_groups`
    paraphrase-matching accepts), then check that at least
    `minimum_support` of `required_evidence_ids` are present in that
    hypothesis's own `supporting_evidence_ids`.

    The first matching produced hypothesis wins (not the best-supported
    one) -- concepts in a well-formed dataset should be specific enough that
    more than one produced hypothesis matching the same concept is unusual;
    if it happens, this is intentionally simple rather than adding a
    best-match scoring pass for a "first version" evaluator.
    """
    results: list[HypothesisMatchResult] = []
    for expectation in expected:
        match = next(
            (
                h
                for h in produced
                if normalize_text(expectation.concept) in normalize_text(h.description)
            ),
            None,
        )
        if match is None:
            results.append(
                HypothesisMatchResult(
                    expectation, matched=False, matched_hypothesis=None, support_satisfied=False
                )
            )
            continue

        support_count = sum(
            1 for evidence_id in expectation.required_evidence_ids
            if evidence_id in match.supporting_evidence_ids
        )
        results.append(
            HypothesisMatchResult(
                expectation,
                matched=True,
                matched_hypothesis=match,
                support_satisfied=support_count >= expectation.minimum_support,
            )
        )
    return results


def find_unsupported_hypotheses(
    produced: list[RootCauseHypothesis], gathered_evidence: list[EvidenceItem]
) -> list[RootCauseHypothesis]:
    """Hypotheses whose claimed `supporting_evidence_ids` share NOTHING with
    the evidence that was actually gathered -- a hypothesis citing support
    that doesn't exist in this investigation at all, the clearest
    deterministically-detectable case of "unsupported" (a hypothesis with
    *some* real support but not enough is a `HypothesisMatchResult` failure
    above, not this function's concern).
    """
    gathered_references = {item.reference for item in gathered_evidence}
    return [
        hypothesis
        for hypothesis in produced
        if not (set(hypothesis.supporting_evidence_ids) & gathered_references)
    ]
