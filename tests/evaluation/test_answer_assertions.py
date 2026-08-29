"""Tests for `app.evaluation.assertions.answer` -- all 7 assertion types."""

from __future__ import annotations

import pytest

from app.evaluation.adapters.semantic import DEFAULT_SIMILARITY_SCORER
from app.evaluation.assertions.answer import evaluate_all, evaluate_assertion
from app.evaluation.schemas import AnswerAssertion


@pytest.mark.asyncio
async def test_exact_match_passes_on_normalized_equality():
    assertion = AnswerAssertion(type="exact_match", value="Hello World")
    result = await evaluate_assertion("  hello   world  ", assertion)
    assert result.passed


@pytest.mark.asyncio
async def test_exact_match_fails_on_different_text():
    assertion = AnswerAssertion(type="exact_match", value="Hello World")
    result = await evaluate_assertion("Goodbye World", assertion)
    assert not result.passed


@pytest.mark.asyncio
async def test_contains_passes_when_substring_present():
    assertion = AnswerAssertion(type="contains", value="connection pool")
    result = await evaluate_assertion("The DB connection pool was exhausted.", assertion)
    assert result.passed


@pytest.mark.asyncio
async def test_contains_fails_when_substring_absent():
    assertion = AnswerAssertion(type="contains", value="memory leak")
    result = await evaluate_assertion("The DB connection pool was exhausted.", assertion)
    assert not result.passed


@pytest.mark.asyncio
async def test_contains_any_passes_with_one_match():
    assertion = AnswerAssertion(type="contains_any", value=["memory leak", "connection pool"])
    result = await evaluate_assertion("The DB connection pool was exhausted.", assertion)
    assert result.passed


@pytest.mark.asyncio
async def test_contains_any_fails_with_no_matches():
    assertion = AnswerAssertion(type="contains_any", value=["memory leak", "disk full"])
    result = await evaluate_assertion("The DB connection pool was exhausted.", assertion)
    assert not result.passed


@pytest.mark.asyncio
async def test_contains_all_passes_when_every_value_present():
    assertion = AnswerAssertion(type="contains_all", value=["connection pool", "deployment 456"])
    result = await evaluate_assertion(
        "Deployment 456 exhausted the connection pool.", assertion
    )
    assert result.passed


@pytest.mark.asyncio
async def test_contains_all_fails_when_one_value_missing():
    assertion = AnswerAssertion(type="contains_all", value=["connection pool", "memory leak"])
    result = await evaluate_assertion("The connection pool was exhausted.", assertion)
    assert not result.passed


@pytest.mark.asyncio
async def test_forbidden_content_passes_when_absent():
    assertion = AnswerAssertion(type="forbidden_content", value="billing")
    result = await evaluate_assertion("The connection pool was exhausted.", assertion)
    assert result.passed


@pytest.mark.asyncio
async def test_forbidden_content_fails_when_present():
    assertion = AnswerAssertion(type="forbidden_content", value="billing")
    result = await evaluate_assertion("This is unrelated to the billing system.", assertion)
    assert not result.passed


@pytest.mark.asyncio
async def test_regex_passes_on_match():
    assertion = AnswerAssertion(type="regex", value=r"\d+\s+to\s+\d+")
    result = await evaluate_assertion("Reduced from 100 to 10 connections.", assertion)
    assert result.passed


@pytest.mark.asyncio
async def test_regex_fails_without_match():
    assertion = AnswerAssertion(type="regex", value=r"\d+\s+to\s+\d+")
    result = await evaluate_assertion("Reduced significantly.", assertion)
    assert not result.passed


@pytest.mark.asyncio
async def test_regex_invalid_pattern_fails_cleanly_not_raises():
    assertion = AnswerAssertion(type="regex", value=r"[")
    result = await evaluate_assertion("anything", assertion)
    assert not result.passed


@pytest.mark.asyncio
async def test_semantic_similarity_passes_on_high_token_overlap():
    assertion = AnswerAssertion(type="semantic_similarity", value="connection pool exhausted")
    result = await evaluate_assertion(
        "the connection pool exhausted", assertion, similarity_scorer=DEFAULT_SIMILARITY_SCORER
    )
    assert result.passed


@pytest.mark.asyncio
async def test_semantic_similarity_fails_below_threshold():
    assertion = AnswerAssertion(
        type="semantic_similarity", value="connection pool exhausted", threshold=0.9
    )
    result = await evaluate_assertion(
        "totally different words entirely",
        assertion,
        similarity_scorer=DEFAULT_SIMILARITY_SCORER,
    )
    assert not result.passed


def test_assertion_value_shape_validated_for_list_types():
    with pytest.raises(ValueError):
        AnswerAssertion(type="contains_any", value="not-a-list")


def test_assertion_value_shape_validated_for_scalar_types():
    with pytest.raises(ValueError):
        AnswerAssertion(type="contains", value=["should", "be", "a", "string"])


@pytest.mark.asyncio
async def test_evaluate_all_does_not_short_circuit_on_first_failure():
    assertions = [
        AnswerAssertion(type="contains", value="missing-text"),
        AnswerAssertion(type="contains", value="also-missing"),
    ]
    results = await evaluate_all("nothing relevant here", assertions)
    assert len(results) == 2
    assert all(not result.passed for _assertion, result in results)
