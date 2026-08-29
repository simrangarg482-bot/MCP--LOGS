"""The 7 deterministic (plus one optionally-semantic) answer-text assertion
types. `evaluate_assertion` is the single dispatch point the runner calls;
every assertion type is otherwise a small, independently testable pure
function (or, for `semantic_similarity`, an async one -- the only assertion
type that may call out to a scorer).
"""

from __future__ import annotations

import re

from app.evaluation.adapters.semantic import DEFAULT_SIMILARITY_SCORER, SimilarityScorer
from app.evaluation.schemas import AnswerAssertion, normalize_text

#: Default similarity threshold for `semantic_similarity` when the
#: assertion itself doesn't set one -- configurable per-assertion via
#: `AnswerAssertion.threshold`, this is only the fallback.
DEFAULT_SEMANTIC_SIMILARITY_THRESHOLD = 0.5


class AssertionResult:
    def __init__(self, passed: bool, reason: str) -> None:
        self.passed = passed
        self.reason = reason


def _exact_match(answer: str, expected: str) -> AssertionResult:
    passed = normalize_text(answer) == normalize_text(expected)
    return AssertionResult(passed, "exact match" if passed else f"expected exactly {expected!r}")


def _contains(answer: str, expected: str) -> AssertionResult:
    passed = normalize_text(expected) in normalize_text(answer)
    return AssertionResult(passed, "found" if passed else f"missing expected text {expected!r}")


def _contains_any(answer: str, expected: list[str]) -> AssertionResult:
    normalized_answer = normalize_text(answer)
    passed = any(normalize_text(item) in normalized_answer for item in expected)
    reason = "found at least one" if passed else f"none of {expected!r} found"
    return AssertionResult(passed, reason)


def _contains_all(answer: str, expected: list[str]) -> AssertionResult:
    normalized_answer = normalize_text(answer)
    missing = [item for item in expected if normalize_text(item) not in normalized_answer]
    reason = "all found" if not missing else f"missing {missing!r}"
    return AssertionResult(not missing, reason)


def _forbidden_content(answer: str, forbidden: str) -> AssertionResult:
    present = normalize_text(forbidden) in normalize_text(answer)
    reason = "clean" if not present else f"forbidden text {forbidden!r} present"
    return AssertionResult(not present, reason)


def _regex(answer: str, pattern: str) -> AssertionResult:
    try:
        matched = re.search(pattern, answer) is not None
    except re.error as exc:
        return AssertionResult(False, f"invalid regex {pattern!r}: {exc}")
    reason = "matched" if matched else f"no match for pattern {pattern!r}"
    return AssertionResult(matched, reason)


async def _semantic_similarity(
    answer: str, expected: str, threshold: float | None, scorer: SimilarityScorer
) -> AssertionResult:
    effective_threshold = (
        threshold if threshold is not None else DEFAULT_SEMANTIC_SIMILARITY_THRESHOLD
    )
    score = await scorer.similarity(answer, expected)
    passed = score >= effective_threshold
    return AssertionResult(
        passed, f"similarity={score:.3f} threshold={effective_threshold:.3f}"
    )


async def evaluate_assertion(
    answer: str,
    assertion: AnswerAssertion,
    *,
    similarity_scorer: SimilarityScorer = DEFAULT_SIMILARITY_SCORER,
) -> AssertionResult:
    """Dispatch one `AnswerAssertion` against `answer`. Always async (even
    though 6 of the 7 types are pure/synchronous under the hood) so the
    runner has one uniform call shape regardless of assertion type --
    matching this codebase's own "the exception, not the rule, drives the
    signature" convention `app.ingestion.connectors.base.Connector.fetch_batch`
    already sets by always being `async def` even for connectors with no
    real per-page I/O.
    """
    if assertion.type == "exact_match":
        assert isinstance(assertion.value, str)
        return _exact_match(answer, assertion.value)
    if assertion.type == "contains":
        assert isinstance(assertion.value, str)
        return _contains(answer, assertion.value)
    if assertion.type == "contains_any":
        assert isinstance(assertion.value, list)
        return _contains_any(answer, assertion.value)
    if assertion.type == "contains_all":
        assert isinstance(assertion.value, list)
        return _contains_all(answer, assertion.value)
    if assertion.type == "forbidden_content":
        assert isinstance(assertion.value, str)
        return _forbidden_content(answer, assertion.value)
    if assertion.type == "regex":
        assert isinstance(assertion.value, str)
        return _regex(answer, assertion.value)
    if assertion.type == "semantic_similarity":
        assert isinstance(assertion.value, str)
        return await _semantic_similarity(
            answer, assertion.value, assertion.threshold, similarity_scorer
        )
    # pragma: no cover - schema-enforced (AnswerAssertion.type has no other literal value)
    raise ValueError(f"unknown assertion type: {assertion.type!r}")


async def evaluate_all(
    answer: str,
    assertions: list[AnswerAssertion],
    *,
    similarity_scorer: SimilarityScorer = DEFAULT_SIMILARITY_SCORER,
) -> list[tuple[AnswerAssertion, AssertionResult]]:
    """Evaluate every assertion, returning the full list (not short-
    circuiting on the first failure) -- a failing case's report should show
    every assertion that failed, not just the first."""
    results = []
    for assertion in assertions:
        result = await evaluate_assertion(answer, assertion, similarity_scorer=similarity_scorer)
        results.append((assertion, result))
    return results
