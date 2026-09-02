"""Tests for `app.mcp.dispatch.run_mcp_tool` -- the shared plumbing every
MCP tool handler (including the new Milestone 8 ones in `app.mcp.tools`)
routes through.

Deliberately does NOT test the tool-handler modules themselves
(`app.mcp.tools.ask_question` etc.): those are decorated with
`@mcp_server.tool()`, and `app.mcp.servers.server`'s own module docstring
already flags that this project's sandbox could never confirm the installed
`mcp` package's exact `Context`/decorator behavior. Testing `run_mcp_tool`
directly avoids that uncertainty entirely -- it is plain `AsyncSession`/
`Identity`-typed code with no FastMCP-specific surface, exercised here with
a fake `session_factory` (matching the shape `scripts/run_mcp_server.py`
installs in production) and monkeypatched `resolve_mcp_identity`/
`record_mcp_request`.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest

from app.core.exceptions import NotFoundError, RateLimitedError
from app.mcp import dispatch as dispatch_module
from app.mcp import rate_limit as rate_limit_module
from app.mcp.dispatch import McpServerNotReadyError, run_mcp_tool
from app.mcp.servers import server as server_module
from app.shared.schemas import Identity


class _FakeSession:
    """Stand-in for `AsyncSession` -- `run_mcp_tool` never calls anything on
    it directly, only passes it through to `resolve_mcp_identity`/`handler`.
    """


@asynccontextmanager
async def _fake_session_factory():
    yield _FakeSession()


async def _fake_set_tenant_context(session, organization_id) -> None:
    """Default fake for `server_module.set_tenant_context` -- most tests
    below only care that `run_mcp_tool` doesn't blow up calling it, not what
    it does; `test_sets_tenant_context_before_calling_handler` below is the
    one that actually asserts on its arguments.
    """


@pytest.fixture(autouse=True)
def _reset_session_factory():
    """`session_factory`/`set_tenant_context` are module-level attributes
    mutated by `scripts/run_mcp_server.py` in production; reset both after
    every test so one test's fixture never leaks into another (there is no
    fixture/DI system for these module-level globals). `set_tenant_context`
    defaults to a no-op fake (Milestone 10) rather than staying `None`, so
    every pre-existing test below keeps working without individually opting
    in to the RLS wiring.
    """
    original_session_factory = server_module.session_factory
    original_set_tenant_context = server_module.set_tenant_context
    server_module.set_tenant_context = _fake_set_tenant_context
    yield
    server_module.session_factory = original_session_factory
    server_module.set_tenant_context = original_set_tenant_context


@pytest.fixture()
def fake_identity(monkeypatch) -> Identity:
    identity = Identity.for_agent("test_agent", uuid.uuid4())

    async def fake_resolve_mcp_identity(session, raw_token):
        assert raw_token == "a-valid-token"
        return identity

    monkeypatch.setattr(dispatch_module, "resolve_mcp_identity", fake_resolve_mcp_identity)
    return identity


@pytest.fixture()
def fake_observability(monkeypatch):
    recorded: list[dict[str, object]] = []

    async def fake_record_mcp_request(session, **kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(
        dispatch_module.observability_service, "record_mcp_request", fake_record_mcp_request
    )
    return recorded


@pytest.mark.asyncio
async def test_raises_when_session_factory_not_set() -> None:
    server_module.session_factory = None

    async def handler(session, identity):
        return "unreachable"

    with pytest.raises(McpServerNotReadyError):
        await run_mcp_tool(
            tool_name="ask_question",
            raw_token="a-valid-token",
            request_summary={},
            handler=handler,
        )


@pytest.mark.asyncio
async def test_raises_when_set_tenant_context_not_set() -> None:
    server_module.session_factory = _fake_session_factory
    server_module.set_tenant_context = None

    async def handler(session, identity):
        return "unreachable"

    with pytest.raises(McpServerNotReadyError):
        await run_mcp_tool(
            tool_name="ask_question",
            raw_token="a-valid-token",
            request_summary={},
            handler=handler,
        )


@pytest.mark.asyncio
async def test_sets_tenant_context_before_calling_handler(fake_identity) -> None:
    """Milestone 10 RLS backstop: `run_mcp_tool` must set the session-local
    tenant GUC (via the injected `set_tenant_context`) after resolving
    `Identity` but before `handler` runs, so every RLS-protected table
    `handler` queries is already scoped correctly.
    """
    server_module.session_factory = _fake_session_factory
    calls: list[tuple[object, object]] = []

    async def recording_set_tenant_context(session, organization_id) -> None:
        calls.append((session, organization_id))

    server_module.set_tenant_context = recording_set_tenant_context

    async def handler(session, identity):
        # By the time handler runs, set_tenant_context must have already
        # been called with this identity's organization_id.
        assert calls == [(session, identity.organization_id)]
        return "ok"

    result = await run_mcp_tool(
        tool_name="ask_question", raw_token="a-valid-token", request_summary={}, handler=handler
    )
    assert result == "ok"
    assert len(calls) == 1
    assert calls[0][1] == fake_identity.organization_id


@pytest.mark.asyncio
async def test_successful_call_returns_handler_result_and_logs_200(
    fake_identity, fake_observability
) -> None:
    server_module.session_factory = _fake_session_factory

    async def handler(session, identity):
        assert isinstance(session, _FakeSession)
        assert identity is fake_identity
        return {"answer": "ok"}

    result = await run_mcp_tool(
        tool_name="ask_question",
        raw_token="a-valid-token",
        request_summary={"query": "why?"},
        handler=handler,
    )

    assert result == {"answer": "ok"}
    assert len(fake_observability) == 1
    logged = fake_observability[0]
    assert logged["tool_name"] == "ask_question"
    assert logged["status_code"] == 200
    assert logged["identity"] == fake_identity.audit_tag


@pytest.mark.asyncio
async def test_ekip_error_propagates_and_logs_its_status_hint(
    fake_identity, fake_observability
) -> None:
    server_module.session_factory = _fake_session_factory

    async def handler(session, identity):
        raise NotFoundError("Incident not found.", error_code="incident.not_found")

    with pytest.raises(NotFoundError):
        await run_mcp_tool(
            tool_name="investigate_incident",
            raw_token="a-valid-token",
            request_summary={},
            handler=handler,
        )

    assert fake_observability[0]["status_code"] == 404


@pytest.mark.asyncio
async def test_unexpected_exception_propagates_and_logs_500(
    fake_identity, fake_observability
) -> None:
    server_module.session_factory = _fake_session_factory

    async def handler(session, identity):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await run_mcp_tool(
            tool_name="ask_question",
            raw_token="a-valid-token",
            request_summary={},
            handler=handler,
        )

    assert fake_observability[0]["status_code"] == 500


@pytest.mark.asyncio
async def test_rate_limited_call_is_rejected_and_logged_429(
    fake_identity, fake_observability, monkeypatch
) -> None:
    """`run_mcp_tool` enforces `app.mcp.rate_limit` after resolving identity
    -- the gap the 2026-09-02 audit found (MCP tool calls had no ceiling at
    all). A throttled call must surface as a 429 and never reach `handler`.
    """
    server_module.session_factory = _fake_session_factory
    monkeypatch.setitem(rate_limit_module._PER_TOOL_RPM, "ask_question", 1.0)
    handler_calls = 0

    async def handler(session, identity):
        nonlocal handler_calls
        handler_calls += 1
        return "ok"

    first = await run_mcp_tool(
        tool_name="ask_question", raw_token="a-valid-token", request_summary={}, handler=handler
    )
    assert first == "ok"

    with pytest.raises(RateLimitedError):
        await run_mcp_tool(
            tool_name="ask_question", raw_token="a-valid-token", request_summary={}, handler=handler
        )

    assert handler_calls == 1  # the throttled call never reached the handler
    assert fake_observability[-1]["status_code"] == 429


@pytest.mark.asyncio
async def test_logging_failure_is_swallowed_not_raised(fake_identity, monkeypatch) -> None:
    server_module.session_factory = _fake_session_factory

    async def failing_record_mcp_request(session, **kwargs):
        raise RuntimeError("observability db down")

    monkeypatch.setattr(
        dispatch_module.observability_service, "record_mcp_request", failing_record_mcp_request
    )

    async def handler(session, identity):
        return "ok"

    # The real tool call succeeded; a failure logging that fact must not
    # turn a success into an error for the caller.
    result = await run_mcp_tool(
        tool_name="ask_question", raw_token="a-valid-token", request_summary={}, handler=handler
    )
    assert result == "ok"
