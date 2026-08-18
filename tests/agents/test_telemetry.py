"""Tests for `app.agents.telemetry` (Phase 5.4/5.7)."""

from __future__ import annotations

from langchain_core.callbacks import UsageMetadataCallbackHandler

from app.agents.telemetry import get_estimated_cost_usd, summarize_usage


def test_summarize_usage_returns_empty_dict_when_nothing_captured() -> None:
    handler = UsageMetadataCallbackHandler()

    result = summarize_usage(handler)

    assert result == {}


def test_summarize_usage_maps_single_model_usage() -> None:
    handler = UsageMetadataCallbackHandler()
    handler.usage_metadata["gpt-4o-mini"] = {
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
    }

    result = summarize_usage(handler)

    assert result == {
        "model_used": "gpt-4o-mini",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
    }


def test_summarize_usage_sums_across_multiple_models() -> None:
    handler = UsageMetadataCallbackHandler()
    handler.usage_metadata["gpt-4o-mini"] = {
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
    }
    handler.usage_metadata["gpt-4o"] = {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }

    result = summarize_usage(handler)

    assert result["model_used"] == "gpt-4o+gpt-4o-mini"
    assert result["prompt_tokens"] == 110
    assert result["completion_tokens"] == 55
    assert result["total_tokens"] == 165


def test_get_estimated_cost_usd_known_model() -> None:
    cost = get_estimated_cost_usd("gpt-4o-mini", 1_000_000, 1_000_000)

    assert cost == 0.75  # 0.15 (prompt) + 0.60 (completion)


def test_get_estimated_cost_usd_unknown_model_returns_none() -> None:
    assert get_estimated_cost_usd("some-future-model", 100, 100) is None


def test_get_estimated_cost_usd_missing_inputs_returns_none() -> None:
    assert get_estimated_cost_usd(None, 100, 100) is None
    assert get_estimated_cost_usd("gpt-4o-mini", None, 100) is None
    assert get_estimated_cost_usd("gpt-4o-mini", 100, None) is None


def test_get_estimated_cost_usd_multi_model_string_returns_none() -> None:
    """`summarize_usage` may join model names with "+" -- not a priced key
    in the pricing table, so this must return None, not silently price it
    against the wrong (or no) model.
    """
    assert get_estimated_cost_usd("gpt-4o+gpt-4o-mini", 100, 100) is None
