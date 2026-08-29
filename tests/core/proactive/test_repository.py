"""Tests for `app.core.proactive.repository` -- upsert/reconciliation
branching and evidence-replacement idempotency.

Uses a capturing fake session (same technique
`tests/core/graph/test_repository.py` uses for `upsert_derived_edge`) for
the branching logic, and compiled-SQL inspection for the plain read
queries -- same disclosed limitation as every other repository test in this
codebase: no live Postgres in this suite.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.proactive import repository


def _compile(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def _contains_uuid(sql: str, value: uuid.UUID) -> bool:
    return value.hex in sql.replace("-", "") or str(value) in sql


class _Finding:
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", uuid.uuid4())
        self.organization_id = kwargs["organization_id"]
        self.project_id = kwargs.get("project_id")
        self.finding_type = kwargs.get("finding_type", "recurring_incident_severity")
        self.status = kwargs.get("status", "active")
        self.title = kwargs.get("title", "t")
        self.summary = kwargs.get("summary", "s")
        self.fingerprint = kwargs.get("fingerprint", "fp")
        self.support_count = kwargs.get("support_count", 3)
        self.detector_name = kwargs.get("detector_name", "agent:pattern_detection_agent")
        self.first_seen_at = kwargs.get("first_seen_at", datetime(2026, 1, 1, tzinfo=UTC))
        self.last_seen_at = kwargs.get("last_seen_at", datetime(2026, 1, 1, tzinfo=UTC))
        self.deactivated_at = kwargs.get("deactivated_at")


class _CapturingSession:
    def __init__(self, scalar_results=None):
        self.statements: list[object] = []
        self.added: list[object] = []
        self._scalar_results = list(scalar_results) if scalar_results is not None else None
        self._existing = None

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        if self._scalar_results is not None:
            return _AllResult(self._scalar_results.pop(0) if self._scalar_results else [])
        return _FirstResult(self._existing)

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


class _FirstResult:
    def __init__(self, existing):
        self._existing = existing

    def scalars(self):
        return self

    def first(self):
        return self._existing


class _AllResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


# --- read query shape -------------------------------------------------


@pytest.mark.asyncio
async def test_list_findings_scopes_by_organization():
    session = _CapturingSession(scalar_results=[[]])
    organization_id = uuid.uuid4()
    await repository.list_findings(
        session,
        organization_id=organization_id,
        status=None,
        finding_type=None,
        limit=10,
        offset=0,
    )
    sql = _compile(session.statements[0])
    assert _contains_uuid(sql, organization_id)


@pytest.mark.asyncio
async def test_list_findings_applies_status_and_type_filters_when_given():
    session = _CapturingSession(scalar_results=[[]])
    await repository.list_findings(
        session,
        organization_id=uuid.uuid4(),
        status="active",
        finding_type="recurring_incident_severity",
        limit=10,
        offset=0,
    )
    where = _compile(session.statements[0]).lower()
    assert "status" in where
    assert "finding_type" in where


# --- upsert branching ---------------------------------------------------


_UPSERT_KWARGS = dict(
    organization_id=uuid.uuid4(),
    project_id=uuid.uuid4(),
    finding_type="recurring_incident_severity",
    fingerprint="recurring_incident_severity:proj-1",
    title="3 incidents",
    summary="s",
    support_count=3,
    detector_name="agent:pattern_detection_agent",
    seen_at=datetime(2026, 1, 1, tzinfo=UTC),
)


@pytest.mark.asyncio
async def test_upsert_creates_a_new_row_when_none_exists():
    session = _CapturingSession()
    row, action = await repository.upsert_finding(session, **_UPSERT_KWARGS)
    assert action == "created"
    assert session.added == [row]
    assert row.status == "active"
    assert row.first_seen_at == _UPSERT_KWARGS["seen_at"]
    assert row.last_seen_at == _UPSERT_KWARGS["seen_at"]


@pytest.mark.asyncio
async def test_upsert_reactivates_a_previously_inactive_row():
    existing = _Finding(
        organization_id=_UPSERT_KWARGS["organization_id"],
        fingerprint=_UPSERT_KWARGS["fingerprint"],
        status="inactive",
        deactivated_at=datetime(2026, 1, 2, tzinfo=UTC),
        support_count=1,
    )
    session = _CapturingSession()
    session._existing = existing
    row, action = await repository.upsert_finding(session, **_UPSERT_KWARGS)
    assert action == "reactivated"
    assert row is existing
    assert row.status == "active"
    assert row.deactivated_at is None
    assert row.support_count == 3
    assert session.added == []


@pytest.mark.asyncio
async def test_upsert_is_a_noop_when_nothing_changed():
    existing = _Finding(
        organization_id=_UPSERT_KWARGS["organization_id"],
        fingerprint=_UPSERT_KWARGS["fingerprint"],
        status="active",
        title=_UPSERT_KWARGS["title"],
        summary=_UPSERT_KWARGS["summary"],
        support_count=_UPSERT_KWARGS["support_count"],
    )
    session = _CapturingSession()
    session._existing = existing
    row, action = await repository.upsert_finding(session, **_UPSERT_KWARGS)
    assert action == "unchanged"
    assert row is existing


@pytest.mark.asyncio
async def test_upsert_reports_updated_when_support_count_changes_while_active():
    existing = _Finding(
        organization_id=_UPSERT_KWARGS["organization_id"],
        fingerprint=_UPSERT_KWARGS["fingerprint"],
        status="active",
        title=_UPSERT_KWARGS["title"],
        summary=_UPSERT_KWARGS["summary"],
        support_count=99,
    )
    session = _CapturingSession()
    session._existing = existing
    row, action = await repository.upsert_finding(session, **_UPSERT_KWARGS)
    assert action == "updated"
    assert row.support_count == 3


# --- deactivate -----------------------------------------------------------


@pytest.mark.asyncio
async def test_deactivate_finding_targets_only_active_rows():
    class _RowcountSession(_CapturingSession):
        async def execute(self, statement, *args, **kwargs):
            self.statements.append(statement)
            return _Rowcount(1)

    class _Rowcount:
        def __init__(self, rowcount):
            self.rowcount = rowcount

    session = _RowcountSession()
    await repository.deactivate_finding(session, uuid.uuid4(), deactivated_at=datetime.now(UTC))
    sql = _compile(session.statements[0]).lower().replace(" ", "")
    assert "status='active'" in sql
    assert "status='inactive'" in sql
