"""Live integration tests for EKIP's MCP server.

Every test here goes over real HTTP to a separately-running
`scripts/run_mcp_server.py` process, using the official `mcp` client SDK's
streamable-HTTP transport and `ClientSession` -- the same path a real MCP
client (Claude Desktop, an IDE plugin) takes. Nothing is called in-process,
so transport framing, the `initialize` handshake, bearer-token extraction
from request headers, and JSON-RPC dispatch are all genuinely exercised.

WHAT IS AND IS NOT ASSERTED
    Tool calls invoke the real agent graph against real ingested data and a
    real LLM, so their *content* is non-deterministic and is NOT asserted on.
    What is asserted is the contract: the call succeeds, returns the declared
    shape, and the server records it. Answer quality is the separate concern
    of `tests/rag_validation/`.

RUN
    python scripts/run_mcp_server.py         # terminal 1
    pytest scripts/live_mcp_tests/ -v -s     # terminal 2
"""

from __future__ import annotations

import contextlib
import json
import uuid
from typing import Any

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import func, select

from app.database.models.core_models import Incident
from app.database.models.mcp_models import McpRequest
from app.database.session import session_scope, set_tenant_context

#: Declared in `app/mcp/tools/`. Asserted as a subset, not equality, so
#: adding a tool does not break this suite -- but removing one does.
_EXPECTED_TOOLS = {
    "ask_question",
    "investigate_incident",
    "generate_postmortem",
    "propose_runbook_update",
    "search_similar_incidents",
    "search_recent_changes",
    "create_project",
    "create_invitation",
    "create_access_rule",
    "configure_sso",
}
_EXPECTED_RESOURCE_TEMPLATES = {"incident://{incident_id}", "document://{document_id}"}
_EXPECTED_PROMPTS = {"triage-incident", "draft-postmortem"}


@contextlib.asynccontextmanager
async def _mcp_session(url: str, token: str | None):
    """Open a real MCP client session against `url`.

    `token` is sent as an `Authorization: Bearer` header, which is exactly
    where `app.mcp.servers.server.extract_bearer_token` reads the caller's
    identity from (`ctx.headers`). Passing `None` sends no header at all, to
    exercise the unauthenticated path.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(headers=headers, timeout=120.0) as http:
        async with streamable_http_client(url, http_client=http) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


async def _first_incident_id(organization_id: uuid.UUID) -> uuid.UUID | None:
    async with session_scope() as session:
        await set_tenant_context(session, organization_id)
        return (
            await session.execute(
                select(Incident.id)
                .where(Incident.organization_id == organization_id)
                .order_by(Incident.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


async def _mcp_request_count() -> int:
    async with session_scope() as session:
        return (await session.execute(select(func.count()).select_from(McpRequest))).scalar_one()


def _text_of(content: list[Any]) -> str:
    return "\n".join(getattr(block, "text", "") for block in content)


# --- handshake and capability discovery -----------------------------------


@pytest.mark.asyncio
async def test_initialize_handshake_succeeds(mcp_server, access_token):
    async with _mcp_session(mcp_server, access_token) as session:
        result = await session.initialize()
        assert result.server_info.name == "ekip"
        print(f"PASS: real MCP initialize handshake -- server_info.name={result.server_info.name!r}")


@pytest.mark.asyncio
async def test_tools_list_exposes_every_declared_tool(mcp_server, access_token):
    async with _mcp_session(mcp_server, access_token) as session:
        listed = {tool.name for tool in (await session.list_tools()).tools}

    missing = _EXPECTED_TOOLS - listed
    assert not missing, f"tools/list is missing declared tools: {sorted(missing)}"
    print(f"PASS: tools/list returned {len(listed)} tool(s) over real MCP; all declared tools present")


@pytest.mark.asyncio
async def test_resource_templates_are_advertised(mcp_server, access_token):
    async with _mcp_session(mcp_server, access_token) as session:
        templates = {t.uri_template for t in (await session.list_resource_templates()).resource_templates}

    missing = _EXPECTED_RESOURCE_TEMPLATES - templates
    assert not missing, f"missing resource templates: {sorted(missing)}"
    print(f"PASS: resource templates advertised: {sorted(templates)}")


@pytest.mark.asyncio
async def test_prompts_are_advertised(mcp_server, access_token):
    async with _mcp_session(mcp_server, access_token) as session:
        names = {p.name for p in (await session.list_prompts()).prompts}

    missing = _EXPECTED_PROMPTS - names
    assert not missing, f"missing prompts: {sorted(missing)}"
    print(f"PASS: prompts advertised: {sorted(names)}")


# --- real tool dispatch ---------------------------------------------------


@pytest.mark.asyncio
async def test_ask_question_tool_returns_the_declared_shape(mcp_server, access_token):
    """Runs the real Answer/Investigation graph against real ingested data.
    Content is non-deterministic, so only the contract is asserted.
    """
    async with _mcp_session(mcp_server, access_token) as session:
        result = await session.call_tool(
            "ask_question", {"query": "Why was Google Workspace SSO login failing?"}
        )

    assert not result.is_error, f"ask_question reported an error: {_text_of(result.content)[:300]}"
    payload = json.loads(_text_of(result.content))
    assert "confidence" in payload and "route_taken" in payload
    assert payload["route_taken"] in {"answer", "investigation"}
    print(
        f"PASS: ask_question over real MCP -- route={payload['route_taken']} "
        f"confidence={payload['confidence']:.3f} citations={len(payload.get('citations') or [])}"
    )


@pytest.mark.asyncio
async def test_incident_resource_reads_a_real_incident(mcp_server, access_token, organization_id):
    incident_id = await _first_incident_id(organization_id)
    if incident_id is None:
        pytest.skip(
            "The bootstrapped organization has no incidents, so there is nothing for the "
            "incident:// resource to return. Not an MCP failure -- create one via POST /incidents."
        )

    async with _mcp_session(mcp_server, access_token) as session:
        result = await session.read_resource(f"incident://{incident_id}")

    body = json.loads(result.contents[0].text)
    assert body["id"] == str(incident_id)
    assert body["organization_id"] == str(organization_id)
    print(f"PASS: incident:// resource returned real incident {incident_id} over MCP")


@pytest.mark.asyncio
async def test_triage_incident_prompt_renders(mcp_server, access_token, organization_id):
    incident_id = await _first_incident_id(organization_id)
    if incident_id is None:
        pytest.skip("No incident available to render the triage-incident prompt against.")

    async with _mcp_session(mcp_server, access_token) as session:
        result = await session.get_prompt("triage-incident", {"incident_id": str(incident_id)})

    assert result.messages, "prompt rendered no messages"
    rendered = result.messages[0].content.text
    assert str(incident_id) in rendered
    print(f"PASS: triage-incident prompt rendered over MCP ({len(rendered)} chars)")


# --- authentication -------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_call_with_invalid_token_is_rejected(mcp_server):
    """A bad bearer token must not reach business logic. The server surfaces
    this as a tool error rather than a transport failure, so assert on the
    tool result rather than expecting an exception.
    """
    async with _mcp_session(mcp_server, "not-a-real-jwt") as session:
        result = await session.call_tool("ask_question", {"query": "should never run"})

    body = _text_of(result.content).lower()
    assert result.is_error or "auth" in body or "token" in body or "unauthor" in body, (
        f"invalid token was NOT rejected; server returned: {_text_of(result.content)[:300]}"
    )
    print("PASS: invalid bearer token rejected by the real MCP server")


# --- observability side effect -------------------------------------------


@pytest.mark.asyncio
async def test_tool_calls_are_recorded_in_mcp_requests(mcp_server, access_token):
    """Every MCP tool call must land in `mcp_requests` -- the table whose
    absent migration previously made `GET /observability/mcp` return HTTP
    500. This asserts the real write path, not a mocked one.
    """
    before = await _mcp_request_count()

    async with _mcp_session(mcp_server, access_token) as session:
        # `description`, not `query` -- see the tool's own signature. A wrong
        # argument name fails FastMCP's validation *before* reaching
        # `run_mcp_tool`, so nothing would be logged and this test would fail
        # on the count while the real logging path was fine. Assert the call
        # itself succeeded first so that mistake reports itself directly.
        result = await session.call_tool(
            "search_similar_incidents", {"description": "checkout returning 500 errors"}
        )
    assert not result.is_error, (
        f"the tool call itself failed, so nothing reached the logging path: "
        f"{_text_of(result.content)[:300]}"
    )

    after = await _mcp_request_count()
    assert after > before, (
        f"mcp_requests did not grow ({before} -> {after}); the MCP request-logging path is broken"
    )
    print(f"PASS: mcp_requests grew {before} -> {after} after a real tool call")
