"""The optional Mode 3 (live) LLM-judge seam.

`is_available()` is the mechanism that makes "skipped cleanly when
credentials are unavailable" real rather than aspirational: the runner calls
it before ever attempting a judge call, and `Settings.openai_api_key` is a
required field with no default (`app.shared.config.settings`), so
`get_settings()` itself can raise when no `.env`/environment configuration
exists at all -- `is_available()` catches that rather than letting a Mode 1
or Mode 2 run crash on an unrelated missing setting.

Uses EKIP's own existing `app.agents.llm.get_llm()` (the one place
`ChatOpenAI` is constructed anywhere in this codebase -- see that module),
not a second, evaluation-only LLM client. This does mean the one real
implementation here is OpenAI-shaped today, same as the rest of the
codebase; the `LLMJudge` protocol itself names no provider, so a future
`get_llm()` provider change carries through automatically, and a
different-provider implementation could be added here without changing the
protocol.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMJudge(Protocol):
    def is_available(self) -> bool:
        """Whether a real judge call can be made right now (credentials
        configured). Always check this before calling `judge()` -- a judge
        implementation is free to assume it will not be called otherwise."""
        ...

    async def judge(self, question: str, context: str) -> bool:
        """Return whether `context` supports a confident answer to
        `question`. Only ever called when `is_available()` is `True`."""
        ...

    def model_info(self) -> tuple[str | None, str | None]:
        """`(provider, model_name)` for the report's provenance fields --
        `(None, None)` when unavailable."""
        ...


class NullLLMJudge:
    """The Mode 1/2 default: always unavailable, never called. Exists so
    every code path can hold an `LLMJudge` reference unconditionally rather
    than threading `LLMJudge | None` through the runner.
    """

    def is_available(self) -> bool:
        return False

    async def judge(self, question: str, context: str) -> bool:
        raise RuntimeError("NullLLMJudge has no live model -- check is_available() first")

    def model_info(self) -> tuple[str | None, str | None]:
        return (None, None)


class RealLLMJudge:
    """Mode 3: a single, targeted yes/no call via EKIP's own `get_llm()` --
    same shape as `app.agents.answer.grounding._llm_grounding_check`'s
    escalation call, reused as a pattern (not imported: that function is
    module-private and answers a different question -- "is this sentence
    grounded in these chunks" vs. this judge's "does this context support a
    confident answer to this question").
    """

    def is_available(self) -> bool:
        try:
            from app.shared.config.settings import get_settings

            settings = get_settings()
        except Exception:
            return False
        return bool(settings.openai_api_key and settings.openai_api_key.strip())

    async def judge(self, question: str, context: str) -> bool:
        from app.agents.llm import get_llm
        from app.agents.prompt_safety import build_messages

        llm = get_llm(temperature=0.0)
        messages = build_messages(
            system_instructions=(
                "Does the evidence below support a confident, specific answer to the "
                "question? Answer with exactly one word: yes or no."
            ),
            evidence_block=context,
            task=f"Question: {question}",
        )
        response = await llm.ainvoke(messages)
        return str(response.content).strip().lower().startswith("y")

    def model_info(self) -> tuple[str | None, str | None]:
        try:
            from app.shared.config.settings import get_settings

            settings = get_settings()
        except Exception:
            return (None, None)
        return ("openai", settings.agent_llm_model)


DEFAULT_LLM_JUDGE: LLMJudge = NullLLMJudge()
