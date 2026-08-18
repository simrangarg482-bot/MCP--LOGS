"""Tests for `app.agents.llm.get_llm` (Phase 6.1).

Regression coverage for a real, confirmed gap: `ChatOpenAI`'s own
`request_timeout` field defaults to `None`, and langchain-openai forwards
that literal `None` straight to the `openai` SDK client as `timeout=None` --
unlike simply omitting the parameter, this actively *disables* the SDK's
own default timeout (`httpx`'s meaning of `timeout=None`), letting a call
hang indefinitely. `get_llm()` must always pass an explicit, finite timeout.
"""

from __future__ import annotations

from app.agents.llm import get_llm


def test_get_llm_sets_a_finite_request_timeout() -> None:
    get_llm.cache_clear()
    llm = get_llm()

    assert llm.request_timeout is not None
    assert llm.request_timeout > 0


def test_get_llm_does_not_disable_timeout_with_none() -> None:
    """Pins the exact failure mode down: `request_timeout` must never be
    `None`, since that specific value is what silently disables the
    underlying SDK's timeout rather than falling back to a sane default.
    """
    get_llm.cache_clear()
    llm = get_llm()

    assert llm.request_timeout is not None
