"""Tests for `app.ingestion.connectors.incidents` -- audit finding 6's
`IncidentsConnector`. Mirrors `tests/ingestion/connectors/test_runbooks.py`'s
style: monkeypatches `incidents_reads.list_incidents_for_ingestion` and
`session_scope` rather than hitting a real database.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from app.core.incidents.schemas import Incident
from app.ingestion.connectors import incidents as incidents_module
from app.ingestion.connectors.incidents import IncidentsConnector, _IncidentsClient
from app.ingestion.schemas import ResolvedConnectorConfig
from app.shared.schemas import Identity


def _incident(
    organization_id: uuid.UUID,
    *,
    status: str = "closed",
    severity: str = "high",
    title: str = "Checkout returning 500 errors",
    description: str = "Customers could not complete checkout for ~40 minutes.",
) -> Incident:
    now = datetime.now(timezone.utc)
    return Incident(
        id=uuid.uuid4(),
        organization_id=organization_id,
        project_id=uuid.uuid4(),
        title=title,
        description=description,
        status=status,
        severity=severity,
        owner_team=None,
        reported_by=uuid.uuid4(),
        resolved_at=now,
        created_at=now,
        updated_at=now,
    )


def _patch_session_scope(monkeypatch) -> None:
    @asynccontextmanager
    async def fake_session_scope():
        yield None

    monkeypatch.setattr(incidents_module, "session_scope", fake_session_scope)


# --- authenticate --------------------------------------------------------------


@pytest.mark.asyncio
async def test_authenticate_builds_client_with_no_network_call() -> None:
    connector = IncidentsConnector()
    organization_id = uuid.uuid4()
    config = ResolvedConnectorConfig(
        connector_config_id=uuid.uuid4(),
        organization_id=organization_id,
        project_id=None,
        source="incidents",
        credential_ref="unused",
        config={},
    )

    client = await connector.authenticate(config)

    assert client.organization_id == organization_id
    assert isinstance(client.actor, Identity)
    assert client.actor.audit_tag == "agent:ingestion_worker"


# --- fetch_batch -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_batch_pairs_incident_with_root_cause(monkeypatch) -> None:
    _patch_session_scope(monkeypatch)
    organization_id = uuid.uuid4()
    incident = _incident(organization_id)

    async def fake_list(session, org_id, *, since, offset, limit):
        assert org_id == organization_id
        assert offset == 0
        return [(incident, "A null pointer in the checkout handler.")]

    monkeypatch.setattr(
        incidents_module.incidents_reads, "list_incidents_for_ingestion", fake_list
    )

    connector = IncidentsConnector()
    client = _IncidentsClient(
        organization_id=organization_id,
        actor=Identity.for_agent("ingestion_worker", organization_id),
    )

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert len(result.items) == 1
    assert result.items[0]["id"] == str(incident.id)
    assert result.items[0]["root_cause"] == "A null pointer in the checkout handler."
    assert result.has_more is False
    assert result.next_cursor is None


@pytest.mark.asyncio
async def test_fetch_batch_handles_incident_with_no_resolution(monkeypatch) -> None:
    """Requirement 8: incidents without a resolution must still be fetched
    (and, downstream, still indexed) -- `root_cause` is simply `None`.
    """
    _patch_session_scope(monkeypatch)
    organization_id = uuid.uuid4()
    incident = _incident(organization_id, status="open")

    async def fake_list(session, org_id, *, since, offset, limit):
        return [(incident, None)]

    monkeypatch.setattr(
        incidents_module.incidents_reads, "list_incidents_for_ingestion", fake_list
    )

    connector = IncidentsConnector()
    client = _IncidentsClient(
        organization_id=organization_id,
        actor=Identity.for_agent("ingestion_worker", organization_id),
    )

    result = await connector.fetch_batch(client, since=None, cursor=None)

    assert result.items[0]["root_cause"] is None


@pytest.mark.asyncio
async def test_fetch_batch_full_page_advances_offset(monkeypatch) -> None:
    _patch_session_scope(monkeypatch)
    organization_id = uuid.uuid4()
    pairs = [(_incident(organization_id), None) for _ in range(50)]  # exactly _PAGE_SIZE

    async def fake_list(session, org_id, *, since, offset, limit):
        return pairs

    monkeypatch.setattr(
        incidents_module.incidents_reads, "list_incidents_for_ingestion", fake_list
    )

    connector = IncidentsConnector()
    client = _IncidentsClient(
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
        incidents_module.incidents_reads, "list_incidents_for_ingestion", fake_list
    )

    connector = IncidentsConnector()
    client = _IncidentsClient(
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


def test_normalize_with_root_cause_includes_resolution() -> None:
    connector = IncidentsConnector()
    organization_id = uuid.uuid4()
    incident = _incident(organization_id)
    raw_item = {
        **incident.model_dump(mode="json"),
        "root_cause": "A null pointer in the checkout handler.",
    }

    doc = connector.normalize(raw_item)

    assert doc.source == "incidents"
    assert doc.external_id == str(incident.id)
    assert doc.title == incident.title
    assert incident.title in doc.content
    assert incident.description in doc.content
    assert "Resolution: A null pointer in the checkout handler." in doc.content
    assert doc.source_url == f"/incidents/{incident.id}"
    assert doc.metadata["incident_id"] == str(incident.id)
    assert doc.metadata["status"] == incident.status
    assert doc.metadata["severity"] == incident.severity


def test_normalize_without_root_cause_omits_resolution_gracefully() -> None:
    """Requirement 8: resolution is not mandatory to index/search an
    incident -- normalize() must not error, and must not fabricate a
    "Resolution:" section, when `root_cause` is `None`.
    """
    connector = IncidentsConnector()
    organization_id = uuid.uuid4()
    incident = _incident(organization_id, status="open")
    raw_item = {**incident.model_dump(mode="json"), "root_cause": None}

    doc = connector.normalize(raw_item)

    assert "Resolution:" not in doc.content
    assert incident.title in doc.content
    assert incident.description in doc.content


# --- close / cursor ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_is_a_no_op() -> None:
    connector = IncidentsConnector()
    organization_id = uuid.uuid4()
    client = _IncidentsClient(
        organization_id=organization_id,
        actor=Identity.for_agent("ingestion_worker", organization_id),
    )
    assert await connector.close(client) is None


def test_decode_cursor_defaults_to_zero() -> None:
    assert IncidentsConnector._decode_cursor(None) == 0


def test_decode_cursor_parses_envelope() -> None:
    assert IncidentsConnector._decode_cursor(json.dumps({"offset": 42})) == 42
