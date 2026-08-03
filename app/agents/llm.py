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
    )
