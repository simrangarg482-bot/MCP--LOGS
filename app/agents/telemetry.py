"""AI usage/cost telemetry helpers (Phase 5.4/5.7).

Owned by: agents/. One shared place to attach LangChain's own
`UsageMetadataCallbackHandler` around an agent execution and collapse
whatever it captured into the four columns `agent_executions` now carries
(`model_used`, `prompt_tokens`, `completion_tokens`, `total_tokens`) --
rather than every call site in `app/agents/service.py` reimplementing this.

Deliberately does not persist or log any prompt/completion *content* --
only token counts and the model name, matching `AgentExecution.
input_summary`'s own existing "structured summary, never full context"
discipline (see `app.database.models.agent_models`'s module docstring).
"""

from __future__ import annotations

from langchain_core.callbacks import UsageMetadataCallbackHandler


def summarize_usage(handler: UsageMetadataCallbackHandler) -> dict[str, object]:
    """Collapse `handler.usage_metadata` (a `dict[model_name, UsageMetadata]`
    LangChain accumulates across every LLM call made while the handler was
    attached) into a single flat dict suitable for `**kwargs` into
    `repository.update_agent_execution`.

    Returns an empty dict (not a dict of `None`s) if no usage was captured
    at all -- e.g. every LLM call within this execution was mocked, or the
    installed model provider doesn't populate `usage_metadata` -- so the
    caller's `update_agent_execution(**summarize_usage(handler))` is a
    no-op update for those fields rather than overwriting them with `NULL`
    on a partial/retried execution.

    Today, `app.agents.llm.get_llm()` is a single global model setting, so
    exactly one model name is expected in practice -- but this sums across
    however many keys are actually present (and joins their names) rather
    than assuming exactly one, so a future per-node model change wouldn't
    silently under-count.
    """
    if not handler.usage_metadata:
        return {}

    model_names = sorted(handler.usage_metadata.keys())
    total_prompt = sum(
        usage.get("input_tokens", 0) or 0 for usage in handler.usage_metadata.values()
    )
    total_completion = sum(
        usage.get("output_tokens", 0) or 0 for usage in handler.usage_metadata.values()
    )
    total = sum(usage.get("total_tokens", 0) or 0 for usage in handler.usage_metadata.values())

    return {
        "model_used": "+".join(model_names),
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "total_tokens": total,
    }


# Phase 5.7 (AI usage/cost): USD per 1,000,000 tokens, (prompt, completion) --
# published OpenAI list pricing as of this writing, not this project's actual
# negotiated rate or real billing data. `get_estimated_cost_usd` returns
# `None` for any model not in this table, deliberately, rather than a
# silently wrong guess -- an unpriced model showing "no cost estimate
# available" is honest; assuming it costs the same as `gpt-4o-mini` would not
# be. Update this table if `Settings.agent_llm_model` changes to a model not
# already listed, rather than letting cost estimates silently go missing.
_USD_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
}


def get_estimated_cost_usd(
    model_used: str | None, prompt_tokens: int | None, completion_tokens: int | None
) -> float | None:
    """Estimate USD cost from real, captured token counts and a published
    pricing table -- an estimate, never a substitute for actual billing
    data (which this application has no access to). Returns `None` (not
    `0.0`) whenever any input is missing or the model isn't in
    `_USD_PER_MILLION_TOKENS`, so "unknown" is never confused with "free."

    `model_used` may be a `"+"`-joined multi-model string (see
    `summarize_usage`) if more than one model served one execution -- not
    supported by this pricing lookup today (returns `None` in that case
    too), since no code path in this application actually does that yet.
    """
    if not model_used or prompt_tokens is None or completion_tokens is None:
        return None
    pricing = _USD_PER_MILLION_TOKENS.get(model_used)
    if pricing is None:
        return None
    prompt_price, completion_price = pricing
    return round(
        (prompt_tokens / 1_000_000) * prompt_price
        + (completion_tokens / 1_000_000) * completion_price,
        6,
    )
