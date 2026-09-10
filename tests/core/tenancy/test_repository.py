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


@pytest.mark.asyncio
async def test_ingestion_checkpoint_patch_preserves_status_and_user_config() -> None:
    row = _FakeConnectorConfigRow(status="active")
    row.config = {"repos": [{"repo": "acme/api"}]}
    session = _FakeSession(row)

    result = await repository.patch_connector_config_ingestion_checkpoint(
        session,
        row.id,
        config_patch={"_ingestion_checkpoint": {"cursor": "page-2"}},
    )

    assert result.status == "active"
    assert result.config == {
        "repos": [{"repo": "acme/api"}],
        "_ingestion_checkpoint": {"cursor": "page-2"},
    }


@pytest.mark.asyncio
async def test_ingestion_checkpoint_does_not_mutate_disconnected_connector() -> None:
    row = _FakeConnectorConfigRow(status="disconnected")
    row.config = {"repos": [{"repo": "acme/api"}]}
    session = _FakeSession(row)

    await repository.patch_connector_config_ingestion_checkpoint(
        session,
        row.id,
        config_patch={"_ingestion_checkpoint": {"cursor": "page-2"}},
    )

    assert row.config == {"repos": [{"repo": "acme/api"}]}


# --- get_connector_config_by_source (P1: postmortem-approval auto-ingestion) -


class _ScalarsWrapper:
    """Minimal stand-in for the object `session.execute(stmt)` returns --
    `get_connector_config_by_source` only ever calls `.scalars().first()`
    on it, so this needs to support exactly that.
    """

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class _CapturingSourceLookupSession:
    """Captures the compiled statement `get_connector_config_by_source`
    executes and returns a canned result -- same fake-session-plus-
    compiled-SQL-inspection pattern used throughout this codebase's
    repository tests (no live Postgres in this suite).
    """

    def __init__(self, rows: list) -> None:
        self._rows = rows
        self.compiled_sql: str = ""

    async def execute(self, stmt):
        self.compiled_sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        return _ScalarsWrapper(self._rows)


@pytest.mark.asyncio
async def test_get_connector_config_by_source_returns_matching_row() -> None:
    organization_id = uuid.uuid4()
    row = object()
    session = _CapturingSourceLookupSession([row])

    result = await repository.get_connector_config_by_source(session, organization_id, "runbooks")

    assert result is row
    compact_org_id = organization_id.hex
    assert compact_org_id in session.compiled_sql.replace("-", "") or str(organization_id) in session.compiled_sql
    assert "'runbooks'" in session.compiled_sql


@pytest.mark.asyncio
async def test_get_connector_config_by_source_returns_none_when_absent() -> None:
    session = _CapturingSourceLookupSession([])

    result = await repository.get_connector_config_by_source(session, uuid.uuid4(), "runbooks")

    assert result is None
