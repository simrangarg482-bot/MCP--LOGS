"""Tests for `app.core.tenancy.repository.update_connector_config_sync_status`'s
disconnected-status guard (Phase: connector delete feature).

A connector marked `"disconnected"` (`core.tenancy.service.
disconnect_connector`, the "Delete connector" feature) must stay
disconnected even if a sync that was already running when the user deleted
it later reports success/failure -- `app.ingestion.service.
_execute_ingestion_job` only checks disconnected status once, at the very
start of its own long-lived transaction, so a mid-flight disconnect can't
be caught by that check alone. This is the second, repository-level half of
that same fix.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.tenancy import repository


class _FakeConnectorConfigRow:
    def __init__(self, *, status: str) -> None:
        self.id = uuid.uuid4()
        self.status = status
        self.last_synced_at = None
        self.config: dict = {}


class _FakeSession:
    def __init__(self, row) -> None:
        self._row = row

    async def get(self, model, connector_config_id):
        return self._row

    async def flush(self) -> None:
        return None

    async def refresh(self, row) -> None:
        return None


@pytest.mark.asyncio
async def test_ingestion_success_report_does_not_revive_a_disconnected_connector() -> None:
    row = _FakeConnectorConfigRow(status="disconnected")
    session = _FakeSession(row)

    result = await repository.update_connector_config_sync_status(
        session, row.id, status="active"
    )

    assert result.status == "disconnected"


@pytest.mark.asyncio
async def test_ingestion_failure_report_does_not_revive_a_disconnected_connector() -> None:
    row = _FakeConnectorConfigRow(status="disconnected")
    session = _FakeSession(row)

    result = await repository.update_connector_config_sync_status(
        session, row.id, status="error"
    )

    assert result.status == "disconnected"


@pytest.mark.asyncio
async def test_disconnecting_an_already_disconnected_connector_is_a_no_op() -> None:
    row = _FakeConnectorConfigRow(status="disconnected")
    session = _FakeSession(row)

    result = await repository.update_connector_config_sync_status(
        session, row.id, status="disconnected"
    )

    assert result.status == "disconnected"


@pytest.mark.asyncio
async def test_normal_status_transitions_are_unaffected() -> None:
    row = _FakeConnectorConfigRow(status="connecting")
    session = _FakeSession(row)

    result = await repository.update_connector_config_sync_status(
        session, row.id, status="active"
    )

    assert result.status == "active"
