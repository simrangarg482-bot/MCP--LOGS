"""Tests for `app.evaluation.adapters.llm` -- the "skipped cleanly when
credentials are unavailable" requirement.
"""

from __future__ import annotations

import pytest

from app.evaluation.adapters.llm import NullLLMJudge, RealLLMJudge


def test_null_judge_is_never_available():
    judge = NullLLMJudge()
    assert judge.is_available() is False
    assert judge.model_info() == (None, None)


@pytest.mark.asyncio
async def test_null_judge_raises_if_called_anyway():
    judge = NullLLMJudge()
    with pytest.raises(RuntimeError):
        await judge.judge("question", "context")


def test_real_judge_unavailable_when_settings_raise(monkeypatch):

    def _raise_settings():
        raise RuntimeError("no .env configured")

    monkeypatch.setattr("app.shared.config.settings.get_settings", _raise_settings)
    judge = RealLLMJudge()
    assert judge.is_available() is False
    assert judge.model_info() == (None, None)


def test_real_judge_unavailable_when_api_key_blank(monkeypatch):
    class _FakeSettings:
        openai_api_key = ""
        agent_llm_model = "gpt-4o-mini"

    monkeypatch.setattr("app.shared.config.settings.get_settings", lambda: _FakeSettings())
    judge = RealLLMJudge()
    assert judge.is_available() is False


def test_real_judge_available_when_api_key_present(monkeypatch):
    class _FakeSettings:
        openai_api_key = "sk-fake-test-key"
        agent_llm_model = "gpt-4o-mini"

    monkeypatch.setattr("app.shared.config.settings.get_settings", lambda: _FakeSettings())
    judge = RealLLMJudge()
    assert judge.is_available() is True
    assert judge.model_info() == ("openai", "gpt-4o-mini")
