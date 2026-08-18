"""The one place agents/ constructs an LLM client, per ENGINEERING_DECISIONS.md
#008 (OpenAI, via `langchain-openai`).

Owned by: agents/. Every LLM-calling node -- Retrieval Agent's query
rewriting, Answer Agent's generation, and (Milestone 7) Investigation
Agent's hypothesis generation -- gets its client from `get_llm()` rather
than constructing `ChatOpenAI(...)` directly, so the model name and API key
are configured in exactly one place (`Settings.agent_llm_model`,
`Settings.openai_api_key`) and every node picks up a future model change
identically.

`temperature` is a parameter, not baked into a single cached client, because
different nodes have genuinely different determinism needs already called
out in the docs: query rewriting and hypothesis generation benefit from a
little variance, while a future node wanting near-deterministic output (none
does yet) would want `temperature=0`. `lru_cache` still avoids constructing a
new `ChatOpenAI` (and its underlying HTTP client) on every call for whichever
temperature values actually get used.
"""

from __future__ import annotations

from functools import lru_cache

from langchain_openai import ChatOpenAI

from app.shared.config.settings import get_settings

# Phase 6.1: `ChatOpenAI`'s own `request_timeout` field defaults to `None`,
# and -- unlike simply omitting the parameter -- langchain-openai forwards
# that literal `None` straight to the underlying `openai` SDK client
# (`client_params["timeout"] = self.request_timeout`, always set, never
# conditionally omitted the way `max_retries` is). The `openai` SDK's own
# "substitute my 600s default" logic only fires for its `NotGiven` sentinel,
# not for an explicit `None` -- so leaving this parameter off does not get
# a sane default, it actively disables the timeout entirely (httpx's own
# meaning of `timeout=None`), letting a single LLM call hang indefinitely.
# 60s covers every real prompt shape in this codebase (query rewriting,
# answer generation, hypothesis generation, root-cause extraction) with
# margin -- none of them stream multi-minute completions.
_REQUEST_TIMEOUT_SECONDS = 60.0
# Left unset deliberately (not forwarded as `max_retries=0` or similar):
# `max_retries` is only added to `client_params` when not `None`, and
# leaving it `None` here lets the `openai` SDK apply its own real default
# (`DEFAULT_MAX_RETRIES=2`, exponential backoff, honors `Retry-After`) --
# a policy already reasonable for this codebase's own retry story
# (`app.agents.retry.call_with_retry` layers a further 2 retries with its
# own backoff *on top* of whatever the SDK already did, per that module's
# own docstring on why it retries blindly rather than trying to distinguish
# "the SDK already retried this" from "this is a fresh failure").


@lru_cache
def get_llm(*, temperature: float = 0.2) -> ChatOpenAI:
    """Return a cached `ChatOpenAI` client for `temperature`.

    `temperature=0.2` is a low-but-nonzero default: most agent-node prompts
    in AGENT_WORKFLOWS.md (query rewriting, hypothesis generation, answer
    generation) benefit from *some* variance while still being expected to
    stay close to deterministic, factual output -- not the higher variance
    a creative-writing use case would want.
    """
    settings = get_settings()
    return ChatOpenAI(
        model=settings.agent_llm_model,
        api_key=settings.openai_api_key,
        temperature=temperature,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
