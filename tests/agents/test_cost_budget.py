"""Tests for `app.agents.cost_budget.check_cost_budget` (Phase 6.6)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.agents import cost_budget
from app.core.exceptions import CostBudgetExceededError
from app.shared.config.settings import Settings, get_settings


def _settings_with_budget(budget: float | None) -> Settings:
    settings = get_settings()
    return settings.model_copy(update={"max_organization_cost_usd_per_day": budget})


@pytest.mark.asyncio
async def test_no_op_when_budget_is_unset(monkeypatch) -> None:
    """Default behavior: no enforcement at all -- see the setting's own
    description for why "unset" must never mean "use some default cap."
    """
    monkeypatch.setattr(cost_budget, "get_settings", lambda: _settings_with_budget(None))

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("must not query usage when no budget is configured")

    monkeypatch.setattr(cost_budget.repository, "get_organization_token_usage_since", fail_if_called)

    await cost_budget.check_cost_budget(None, uuid.uuid4())  # must not raise


@pytest.mark.asyncio
async def test_allows_the_call_when_under_budget(monkeypatch) -> None:
    monkeypatch.setattr(cost_budget, "get_settings", lambda: _settings_with_budget(10.0))

    async def fake_usage(session, organization_id, since):
        return (1000, 200)  # well under a $10 budget at gpt-4o-mini pricing

    monkeypatch.setattr(cost_budget.repository, "get_organization_token_usage_since", fake_usage)

    await cost_budget.check_cost_budget(None, uuid.uuid4())  # must not raise


@pytest.mark.asyncio
async def test_raises_when_budget_is_exceeded(monkeypatch) -> None:
    monkeypatch.setattr(cost_budget, "get_settings", lambda: _settings_with_budget(0.0001))

    async def fake_usage(session, organization_id, since):
        return (1_000_000, 1_000_000)  # far exceeds a tiny budget

    monkeypatch.setattr(cost_budget.repository, "get_organization_token_usage_since", fake_usage)

    with pytest.raises(CostBudgetExceededError) as exc_info:
        await cost_budget.check_cost_budget(None, uuid.uuid4())

    assert exc_info.value.status_hint == 429
    assert exc_info.value.error_code == "cost_budget_exceeded"
    assert exc_info.value.detail["estimated_cost_usd"] > exc_info.value.detail["budget_usd"]


@pytest.mark.asyncio
async def test_no_usage_yet_never_raises(monkeypatch) -> None:
    """An organization with zero recorded usage is, correctly, nowhere near
    any budget -- must not be misread as "unknown" and blocked.
    """
    monkeypatch.setattr(cost_budget, "get_settings", lambda: _settings_with_budget(1.0))

    async def fake_usage(session, organization_id, since):
        return (0, 0)

    monkeypatch.setattr(cost_budget.repository, "get_organization_token_usage_since", fake_usage)

    await cost_budget.check_cost_budget(None, uuid.uuid4())  # must not raise


@pytest.mark.asyncio
async def test_unpriced_model_never_raises(monkeypatch) -> None:
    """`get_estimated_cost_usd` returns `None` for a model not in the
    pricing table -- must be treated as "cannot evaluate," never as
    "budget exceeded" (the opposite of the intended fail-safe direction).
    """
    settings = get_settings().model_copy(
        update={"max_organization_cost_usd_per_day": 1.0, "agent_llm_model": "some-future-model"}
    )
    monkeypatch.setattr(cost_budget, "get_settings", lambda: settings)

    async def fake_usage(session, organization_id, since):
        return (1_000_000, 1_000_000)

    monkeypatch.setattr(cost_budget.repository, "get_organization_token_usage_since", fake_usage)

    await cost_budget.check_cost_budget(None, uuid.uuid4())  # must not raise


@pytest.mark.asyncio
async def test_queries_a_rolling_24_hour_window(monkeypatch) -> None:
    monkeypatch.setattr(cost_budget, "get_settings", lambda: _settings_with_budget(10.0))
    captured: dict[str, object] = {}

    async def fake_usage(session, organization_id, since):
        captured["since"] = since
        return (0, 0)

    monkeypatch.setattr(cost_budget.repository, "get_organization_token_usage_since", fake_usage)

    before = datetime.now(timezone.utc)
    await cost_budget.check_cost_budget(None, uuid.uuid4())

    since = captured["since"]
    assert (before - since).total_seconds() < (24 * 3600 + 5)
    assert (before - since).total_seconds() > (24 * 3600 - 5)
