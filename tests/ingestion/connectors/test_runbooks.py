"""Tests for `app.ingestion.connectors.runbooks` -- monkeypatches
`incidents_service.list_postmortems_for_ingestion` and `session_scope`
(this connector's own workaround for the `Connector` protocol having no
`AsyncSession` parameter -- see the module's docstring) rather than hitting
a real database, the same "monkeypatch the module-level dependency" style
used throughout this test suite (e.g. `tests/agents/test_service.py`).
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from app.core.incidents.schemas import ActionItem, Postmortem
from app.ingestion.connectors import runbooks as runbooks_module
from app.ingestion.connectors.runbooks import RunbooksConnector, _RunbooksClient
from app.ingestion.schemas import ResolvedConnectorConfig
from app.shared.schemas import Identity


def _postmortem(
    organization_id: uuid.UUID,
    *,
    root_cause: str | None = "A null pointer in the checkout handler.",
    action_items: list[ActionItem] | None = None,
    status: str = "approved",
) -> Postmortem:
    now = datetime.now(timezone.utc)
    return Postmortem(
        id=uuid.uuid4(),
        organization_id=organization_id,
        incident_id=uuid.uuid4(),
        status=status,
        root_cause=root_cause,
        action_items=action_items or [],
        generated_by="agent:postmortem_agent",
        reviewed_by=uuid.uuid4(),
        created_at=now,
        updated_at=now,
    )


def _patch_session_scope(monkeypatch) -> None:
    @asynccontextmanager
    async def fake_session_scope():
        yield None

    monkeypatch.setattr(runbooks_module, "session_scope", fake_session_scope)


# --- authenticate --------------------------------------------------------------


@pytest.mark.asyncio
async def test_authenticate_builds_client_with_no_network_call() -> None:
    connector = RunbooksConnector()
    organization_id = uuid.uuid4()
    config = ResolvedConnectorConfig(
        connector_config_id=uuid.uuid4(),
        organization_id=organization_id,
        project_id=None,
        source="runbooks",
        credential_ref="unused",
        config={},
    )

    client = await connector.authenticate(config)

    assert client.organization_id == organization_id
    assert isinstance(client.actor, Identity)
    assert client.actor.audit_tag == "agent:ingestion_worker"


# --- fetch_batch -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_batch_partial_page_ends_sync(monkeypatch) -> None:
    _patch_session_scope(monkeypatch)
    organization_id = uuid.uuid4()
    postmortem = _postmortem(organization_id)

    async def fake_list(session, org_id, *, since, offset, limit):
        assert org_id == organization_id
        assert offset == 0
        return [postmortem]

    monkeypatch.setattr(
        runbooks_module.incidents_service, "list_postmortems_for_ingestion", fake_list
    )

    connector = RunbooksConnector()
    client = _RunbooksClient(
        organization_id=organization_id,
        actor=Identity.for_agent("ingestion_worker", organization_id),
    )

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert len(result.items) == 1
    assert result.items[0]["id"] == str(postmortem.id)
    assert result.has_more is False
    assert result.next_cursor is None


@pytest.mark.asyncio
async def test_fetch_batch_full_page_advances_offset(monkeypatch) -> None:
    _patch_session_scope(monkeypatch)
    organization_id = uuid.uuid4()
    postmortems = [_postmortem(organization_id) for _ in range(50)]  # exactly _PAGE_SIZE

    async def fake_list(session, org_id, *, since, offset, limit):
        return postmortems

    monkeypatch.setattr(
        runbooks_module.incidents_service, "list_postmortems_for_ingestion", fake_list
    )

    connector = RunbooksConnector()
    client = _RunbooksClient(
        organization_id=organization_id,
        actor=Identity.for_agent("ingestion_worker", organization_id),
    )

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.has_more is True
    next_state = json.loads(result.next_cursor)
    assert next_state == {"offset": 50}


@pytest.mark.asyncio
async def test_fetch_batch_resumes_from_cursor(monkeypatch) -> None:
    _patch_session_scope(monkeypatch)
    organization_id = uuid.uuid4()
    captured: dict[str, object] = {}

    async def fake_list(session, org_id, *, since, offset, limit):
        captured["offset"] = offset
        return []

    monkeypatch.setattr(
        runbooks_module.incidents_service, "list_postmortems_for_ingestion", fake_list
    )

    connector = RunbooksConnector()
    client = _RunbooksClient(
        organization_id=organization_id,
        actor=Identity.for_agent("ingestion_worker", organization_id),
    )
    cursor = json.dumps({"offset": 100})

    result = await connector.fetch_batch(client, since=None, cursor=cursor)

    assert captured["offset"] == 100
    assert result.items == []
    assert result.has_more is False
    assert result.next_cursor is None


# --- normalize ---------------------------------------------------------------


def test_normalize_with_root_cause_and_action_items() -> None:
    connector = RunbooksConnector()
    organization_id = uuid.uuid4()
    postmortem = _postmortem(
        organization_id,
        action_items=[
            ActionItem(description="Add null check", owner="Jane Doe", status="open"),
            ActionItem(description="Add regression test", owner=None, status="done"),
        ],
    )
    raw_item = postmortem.model_dump(mode="json")

    doc = connector.normalize(raw_item)

    assert doc.source == "runbooks"
    assert doc.external_id == str(postmortem.id)
    assert doc.title == f"Postmortem: incident {postmortem.incident_id}"
    assert "Root cause: A null pointer in the checkout handler." in doc.content
    assert "- [open] Add null check (owner: Jane Doe)" in doc.content
    assert "- [done] Add regression test" in doc.content
    assert "(owner:" not in doc.content.split("Add regression test")[1].split("\n")[0]
    assert doc.source_url is None
    assert doc.metadata["incident_id"] == str(postmortem.incident_id)
    assert doc.metadata["status"] == "approved"
    assert doc.metadata["generated_by"] == "agent:postmortem_agent"


def test_normalize_without_root_cause_uses_placeholder() -> None:
    connector = RunbooksConnector()
    organization_id = uuid.uuid4()
    postmortem = _postmortem(organization_id, root_cause=None)
    raw_item = postmortem.model_dump(mode="json")

    doc = connector.normalize(raw_item)

    assert "Root cause: (no root cause recorded)" in doc.content


def test_normalize_without_action_items_has_no_action_items_section() -> None:
    connector = RunbooksConnector()
    organization_id = uuid.uuid4()
    postmortem = _postmortem(organization_id, action_items=[])
    raw_item = postmortem.model_dump(mode="json")

    doc = connector.normalize(raw_item)

    assert "Action items:" not in doc.content


# --- close / cursor ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_is_a_no_op() -> None:
    connector = RunbooksConnector()
    organization_id = uuid.uuid4()
    client = _RunbooksClient(
        organization_id=organization_id,
        actor=Identity.for_agent("ingestion_worker", organization_id),
    )
    assert await connector.close(client) is None


def test_decode_cursor_defaults_to_zero() -> None:
    assert RunbooksConnector._decode_cursor(None) == 0


def test_decode_cursor_parses_envelope() -> None:
    assert RunbooksConnector._decode_cursor(json.dumps({"offset": 42})) == 42
