"""Tests for `app.core.incidents.reads` -- the narrow, agents-free read
surface split out of `core.incidents.service` in Phase 3 specifically so
`app.ingestion.connectors.runbooks` (its only caller) stops transitively
depending on `app.agents.service` (see `reads.py`'s own module docstring,
and `core.incidents.service.generate_postmortem`'s deferred-import comment
for why that dependency exists in the first place).
"""

from __future__ import annotations

import ast
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.incidents import reads
from app.core.incidents.schemas import Incident, Postmortem


def _postmortem_row(organization_id: uuid.UUID) -> Postmortem:
    now = datetime.now(timezone.utc)
    return Postmortem(
        id=uuid.uuid4(),
        organization_id=organization_id,
        incident_id=uuid.uuid4(),
        status="approved",
        root_cause="A null pointer in the checkout handler.",
        action_items=[],
        generated_by="agent:postmortem_agent",
        reviewed_by=uuid.uuid4(),
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_list_postmortems_for_ingestion_passes_through_and_maps(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    row = _postmortem_row(organization_id)
    captured: dict[str, object] = {}

    async def fake_repo(session, org_id, *, statuses, since, offset, limit):
        captured["organization_id"] = org_id
        captured["statuses"] = statuses
        captured["since"] = since
        captured["offset"] = offset
        captured["limit"] = limit
        return [row]

    monkeypatch.setattr(reads.repository, "list_postmortems_for_ingestion", fake_repo)

    since = datetime(2026, 7, 1, tzinfo=timezone.utc)
    result = await reads.list_postmortems_for_ingestion(
        None, organization_id, since=since, offset=10, limit=25
    )

    assert len(result) == 1
    assert captured["organization_id"] == organization_id
    assert captured["statuses"] == ("approved", "published")
    assert captured["since"] == since
    assert captured["offset"] == 10
    assert captured["limit"] == 25


def _incident_row(organization_id: uuid.UUID) -> Incident:
    now = datetime.now(timezone.utc)
    return Incident(
        id=uuid.uuid4(),
        organization_id=organization_id,
        project_id=uuid.uuid4(),
        title="Checkout returning 500 errors",
        description="Customers could not complete checkout for ~40 minutes.",
        status="closed",
        severity="high",
        owner_team=None,
        reported_by=uuid.uuid4(),
        resolved_at=now,
        created_at=now,
        updated_at=now,
    )


class _IncidentRow:
    """Stand-in for the `Row` SQLAlchemy's `.all()` returns for a
    `select(Incident, Postmortem.root_cause)` query -- `.Incident` and
    `.root_cause` attribute access, same shape `repository.
    list_incidents_for_ingestion` actually returns.
    """

    def __init__(self, incident: Incident, root_cause: str | None) -> None:
        self.Incident = incident
        self.root_cause = root_cause


@pytest.mark.asyncio
async def test_list_incidents_for_ingestion_passes_through_and_maps(monkeypatch) -> None:
    """Audit finding 6: this read surface's new function, mirroring
    `list_postmortems_for_ingestion` immediately above -- a thin,
    agents-free wrapper around `repository.list_incidents_for_ingestion`
    that maps ORM rows to pydantic `Incident` + `root_cause` pairs.
    """
    organization_id = uuid.uuid4()
    incident = _incident_row(organization_id)
    captured: dict[str, object] = {}

    async def fake_repo(session, org_id, *, since, offset, limit):
        captured["organization_id"] = org_id
        captured["since"] = since
        captured["offset"] = offset
        captured["limit"] = limit
        return [_IncidentRow(incident, "A null pointer in the checkout handler.")]

    monkeypatch.setattr(reads.repository, "list_incidents_for_ingestion", fake_repo)

    since = datetime(2026, 7, 1, tzinfo=timezone.utc)
    result = await reads.list_incidents_for_ingestion(
        None, organization_id, since=since, offset=10, limit=25
    )

    assert len(result) == 1
    returned_incident, root_cause = result[0]
    assert returned_incident.id == incident.id
    assert root_cause == "A null pointer in the checkout handler."
    assert captured["organization_id"] == organization_id
    assert captured["since"] == since
    assert captured["offset"] == 10
    assert captured["limit"] == 25


@pytest.mark.asyncio
async def test_list_incidents_for_ingestion_handles_no_resolution(monkeypatch) -> None:
    """Requirement 8: an incident with no approved/published postmortem
    yet must still come back, paired with `root_cause=None`, not filtered
    out or errored on.
    """
    organization_id = uuid.uuid4()
    incident = _incident_row(organization_id)

    async def fake_repo(session, org_id, *, since, offset, limit):
        return [_IncidentRow(incident, None)]

    monkeypatch.setattr(reads.repository, "list_incidents_for_ingestion", fake_repo)

    result = await reads.list_incidents_for_ingestion(
        None, organization_id, since=None, offset=0, limit=50
    )

    assert len(result) == 1
    assert result[0][1] is None


def test_reads_module_never_imports_agents() -> None:
    """Regression guard for the exact bug this module was split out to fix:
    a plain top-level `import`/`from` of anything under `app.agents`
    anywhere in this file -- module-level or deferred inside a function --
    would make `ingestion.connectors.runbooks` transitively depend on
    `agents` again, breaking the "ingestion does not depend on agents or
    mcp" import-linter contract. Checked via AST rather than just re-running
    import-linter here so this fails fast as a normal unit test, independent
    of the separate import-linter CI step.
    """
    source = Path(reads.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("app.agents"), (
                    f"core.incidents.reads must not import {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not module.startswith("app.agents"), (
                f"core.incidents.reads must not import from {module}"
            )
