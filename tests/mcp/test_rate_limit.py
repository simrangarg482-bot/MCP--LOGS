"""`app.mcp.rate_limit.enforce_rate_limit` -- the per-caller, per-tool
request ceiling that closes the gap the 2026-09-02 MCP audit found (MCP
tool calls were entirely unthrottled while their REST equivalents are not).

The shared limiter singleton is reset between tests by the autouse
`_reset_rate_limiters` fixture in the root `tests/conftest.py`.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.exceptions import EKIPError, RateLimitedError
from app.mcp import rate_limit as rate_limit_module
from app.mcp.rate_limit import enforce_rate_limit
from app.shared.schemas import ActorKind, Identity


def _user(org: uuid.UUID | None = None) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=org or uuid.uuid4(),
        user_id=uuid.uuid4(),
    )


def _agent(name: str = "some_agent") -> Identity:
    return Identity.for_agent(name, uuid.uuid4())


async def test_allows_calls_under_the_budget(monkeypatch) -> None:
    monkeypatch.setitem(rate_limit_module._PER_TOOL_RPM, "ask_question", 3.0)
    identity = _user()

    for _ in range(3):
        await enforce_rate_limit("ask_question", identity)  # no raise


async def test_rejects_the_call_that_exceeds_the_budget(monkeypatch) -> None:
    monkeypatch.setitem(rate_limit_module._PER_TOOL_RPM, "ask_question", 2.0)
    identity = _user()

    await enforce_rate_limit("ask_question", identity)
    await enforce_rate_limit("ask_question", identity)
    with pytest.raises(RateLimitedError) as excinfo:
        await enforce_rate_limit("ask_question", identity)

    # RateLimitedError is an EKIPError the MCP boundary maps to 429.
    assert isinstance(excinfo.value, EKIPError)
    assert excinfo.value.status_hint == 429
    assert excinfo.value.error_code == "rate_limited.mcp"
    assert excinfo.value.detail == {"tool": "ask_question"}


async def test_each_tool_has_an_independent_budget(monkeypatch) -> None:
    monkeypatch.setitem(rate_limit_module._PER_TOOL_RPM, "ask_question", 1.0)
    monkeypatch.setitem(rate_limit_module._PER_TOOL_RPM, "investigate_incident", 1.0)
    identity = _user()

    await enforce_rate_limit("ask_question", identity)
    # ask_question is now exhausted, but investigate_incident is untouched
    await enforce_rate_limit("investigate_incident", identity)

    with pytest.raises(RateLimitedError):
        await enforce_rate_limit("ask_question", identity)


async def test_each_caller_has_an_independent_budget(monkeypatch) -> None:
    monkeypatch.setitem(rate_limit_module._PER_TOOL_RPM, "ask_question", 1.0)
    caller_a = _user()
    caller_b = _user()

    await enforce_rate_limit("ask_question", caller_a)
    # caller_b is a different principal -- their bucket is fresh
    await enforce_rate_limit("ask_question", caller_b)

    with pytest.raises(RateLimitedError):
        await enforce_rate_limit("ask_question", caller_a)


async def test_agent_callers_are_keyed_by_subject_not_a_missing_user_id(monkeypatch) -> None:
    """An agent/service `Identity` has `user_id is None`; it must still be
    throttled (keyed by its stable `subject`), not fall through to an
    unbounded path or collide with every other user_id-less caller.
    """
    monkeypatch.setitem(rate_limit_module._PER_TOOL_RPM, "generate_postmortem", 1.0)
    agent_one = _agent("postmortem_agent")
    agent_two = _agent("triage_agent")

    await enforce_rate_limit("generate_postmortem", agent_one)
    await enforce_rate_limit("generate_postmortem", agent_two)  # different subject, own bucket

    with pytest.raises(RateLimitedError):
        await enforce_rate_limit("generate_postmortem", agent_one)


async def test_unlisted_tool_falls_back_to_the_default_budget(monkeypatch) -> None:
    monkeypatch.setattr(rate_limit_module, "_DEFAULT_RPM", 2.0)
    identity = _user()

    await enforce_rate_limit("create_project", identity)
    await enforce_rate_limit("create_project", identity)
    with pytest.raises(RateLimitedError):
        await enforce_rate_limit("create_project", identity)


def test_agent_tools_mirror_their_rest_rate_limits() -> None:
    """`app.api.routers.ask` sets 20/10/30 for question/investigate/search;
    the MCP tools must not be looser than their REST twins.
    """
    assert rate_limit_module._PER_TOOL_RPM["ask_question"] == 20.0
    assert rate_limit_module._PER_TOOL_RPM["investigate_incident"] == 10.0
    assert rate_limit_module._PER_TOOL_RPM["search_similar_incidents"] == 30.0
    assert rate_limit_module._PER_TOOL_RPM["search_recent_changes"] == 30.0
