"""Tests for the Milestone 10 RLS-bypass repository functions in
`app.ingestion.repository` -- `resolve_connector_config_organization_id`,
`resolve_document_organization_id`, `list_active_connector_config_ids`.

Each one issues a single raw SQL statement calling one of the
`SECURITY DEFINER` functions defined in
`d2e5f8a3c1b6_milestone_10_rls_bypass_functions.py`. No real Postgres
connection is available to this test suite, so these tests exercise the
statement/parameter shape against a fake `AsyncSession`, not the actual
bypass behavior against a live database.
"""

from __future__ import annotations

import uuid

import pytest

from app.ingestion import repository


class _FakeScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value


class _FakeSession:
    def __init__(self, return_value) -> None:
        self._return_value = return_value
        self.executed: list[tuple[str, dict[str, object]]] = []

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params or {}))
        return _FakeScalarResult(self._return_value)


class _FakeInsertSession:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.added = None

    async def execute(self, statement, params=None):
        self.executed.append(str(statement))
        return _FakeScalarResult(None)

    def add(self, row) -> None:
        self.added = row

    async def flush(self) -> None:
        return None

    async def refresh(self, row) -> None:
        return None


@pytest.mark.asyncio
async def test_resolve_connector_config_organization_id_calls_bypass_function() -> None:
    organization_id = uuid.uuid4()
    connector_config_id = uuid.uuid4()
    session = _FakeSession(organization_id)

    result = await repository.resolve_connector_config_organization_id(session, connector_config_id)

    assert result == organization_id
    statement, params = session.executed[0]
    assert "resolve_connector_config_organization" in statement
    assert params == {"config_id": connector_config_id}


@pytest.mark.asyncio
async def test_resolve_connector_config_organization_id_returns_none_when_missing() -> None:
    session = _FakeSession(None)

    result = await repository.resolve_connector_config_organization_id(session, uuid.uuid4())

    assert result is None


@pytest.mark.asyncio
async def test_resolve_document_organization_id_calls_bypass_function() -> None:
    organization_id = uuid.uuid4()
    document_id = uuid.uuid4()
    session = _FakeSession(organization_id)

    result = await repository.resolve_document_organization_id(session, document_id)

    assert result == organization_id
    statement, params = session.executed[0]
    assert "resolve_document_organization" in statement
    assert params == {"document_id": document_id}


@pytest.mark.asyncio
async def test_list_active_connector_config_ids_calls_bypass_function() -> None:
    ids = [uuid.uuid4(), uuid.uuid4()]
    session = _FakeSession(ids)

    result = await repository.list_active_connector_config_ids(session)

    assert result == ids
    statement, _params = session.executed[0]
    assert "list_active_connector_config_ids" in statement


@pytest.mark.asyncio
async def test_insert_job_recovers_interrupted_predecessor_first() -> None:
    organization_id = uuid.uuid4()
    connector_config_id = uuid.uuid4()
    session = _FakeInsertSession()

    row = await repository.insert_ingestion_job(
        session,
        organization_id=organization_id,
        connector_config_id=connector_config_id,
    )

    assert row is session.added
    assert row.status == "queued"
    assert row.organization_id == organization_id
    assert row.connector_config_id == connector_config_id
    assert len(session.executed) == 1
    recovery_statement = session.executed[0]
    assert "UPDATE ingestion_jobs" in recovery_statement
    assert "ingestion_jobs.status" in recovery_statement
    assert "ingestion_jobs.connector_config_id" in recovery_statement


# --- document_metadata value fits under the btree index limit ---------------


class _CollectingSession:
    def __init__(self) -> None:
        self.rows: list[object] = []

    def add(self, row) -> None:
        self.rows.append(row)

    async def flush(self) -> None:
        return None


def test_fit_metadata_value_passes_small_values_through() -> None:
    assert repository._fit_metadata_value("a,b,c") == "a,b,c"
    assert repository._fit_metadata_value("x" * 2000) == "x" * 2000


def test_fit_metadata_value_caps_oversized_values_under_the_btree_limit() -> None:
    huge = ",".join(f"path/to/file_{i}.py" for i in range(2000))  # ~40 KB

    fitted = repository._fit_metadata_value(huge)

    assert len(fitted.encode("utf-8")) <= repository._MAX_METADATA_VALUE_BYTES
    assert fitted.endswith(repository._METADATA_TRUNCATION_MARKER)
    assert fitted.startswith("path/to/file_0.py,")


@pytest.mark.asyncio
async def test_insert_document_metadata_fits_every_value() -> None:
    from app.ingestion.schemas import DocumentMetadataEntry

    session = _CollectingSession()
    await repository.insert_document_metadata(
        session,
        document_id=uuid.uuid4(),
        entries=[
            DocumentMetadataEntry(key="repo", value="acme/widgets"),
            DocumentMetadataEntry(key="changed_files", value="f.py," * 5000),
        ],
    )

    assert [r.key for r in session.rows] == ["repo", "changed_files"]
    assert session.rows[0].value == "acme/widgets"
    assert len(session.rows[1].value.encode("utf-8")) <= repository._MAX_METADATA_VALUE_BYTES
