"""Tests for `app.ingestion.service._execute_ingestion_job`'s Milestone 10
decryption wiring -- not a full test suite for `ingestion.service` (no test
infrastructure for that module existed before this addition). Everything
around the fetch/process loop (repository inserts/updates, tenancy sync-
status recording) is monkeypatched to a no-op/fake so this test isolates
exactly one thing: that the connector's `authenticate()` receives the
*decrypted* credential, never the encrypted blob actually stored on the
connector_config row.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.ingestion import service as ingestion_service
from app.ingestion.schemas import FetchResult, ResolvedConnectorConfig
from app.shared.security import encrypt_secret, get_kms


class _FakeNestedTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    """Minimal stand-in for `AsyncSession` -- `_execute_ingestion_job` only
    ever calls `begin_nested()` on it directly (every actual read/write in
    this test goes through monkeypatched `repository`/`tenancy_service`
    functions instead, none of which touch this object at all).
    """

    def begin_nested(self):
        return _FakeNestedTransaction()


class _FakeConnectorConfigRow:
    def __init__(self, *, organization_id, source, credential_ref) -> None:
        self.id = uuid.uuid4()
        self.organization_id = organization_id
        self.project_id = None
        self.source = source
        self.credential_ref = credential_ref
        self.config = {}
        self.last_synced_at = None


class _FakeJobRow:
    def __init__(self, organization_id, connector_config_id) -> None:
        self.id = uuid.uuid4()
        self.organization_id = organization_id
        self.connector_config_id = connector_config_id
        self.status = "queued"
        self.failed_stage = None
        self.documents_processed = 0
        self.started_at = None
        self.completed_at = None
        self.created_at = datetime.now(timezone.utc)


class _RecordingConnector:
    """A minimal fake satisfying `Connector`'s structural shape -- records
    the `credential_ref` it was actually authenticated with, and ends the
    sync immediately (no items), so this test never touches the
    processing/persistence pipeline at all.
    """

    source_name = "fake_source"
    requests_per_second = 10.0

    def __init__(self) -> None:
        self.received_credential_ref: str | None = None
        self.closed = False

    async def authenticate(self, config: ResolvedConnectorConfig):
        self.received_credential_ref = config.credential_ref
        return object()

    async def fetch_batch(self, client, *, since, cursor):
        return FetchResult(items=[], next_cursor=None, has_more=False)

    def normalize(self, raw_item):  # pragma: no cover - never called in this test
        raise AssertionError("normalize should not be called with zero fetched items")

    async def close(self, client) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_execute_ingestion_job_decrypts_credential_before_authenticating(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    plaintext_credential = "xoxb-11725744885042-fake-slack-bot-token"
    encrypted_credential_ref = await encrypt_secret(get_kms(), plaintext_credential)

    config_row = _FakeConnectorConfigRow(
        organization_id=organization_id,
        source="fake_source",
        credential_ref=encrypted_credential_ref,
    )
    fake_connector = _RecordingConnector()

    async def fake_resolve_connector_config_organization_id(session, connector_config_id):
        return organization_id

    async def fake_get_connector_config(session, connector_config_id):
        return config_row

    async def fake_insert_ingestion_job(session, *, organization_id, connector_config_id):
        return _FakeJobRow(organization_id, connector_config_id)

    async def fake_update_ingestion_job(session, job_id, **fields):
        row = _FakeJobRow(organization_id, config_row.id)
        row.id = job_id
        for key, value in fields.items():
            setattr(row, key, value)
        return row

    async def fake_update_connector_sync_status(session, actor, org_id, connector_config_id, **kwargs):
        return None

    async def fake_set_tenant_context(session, org_id) -> None:
        return None

    monkeypatch.setitem(ingestion_service._CONNECTOR_REGISTRY, "fake_source", fake_connector)
    monkeypatch.setattr(
        ingestion_service.repository,
        "resolve_connector_config_organization_id",
        fake_resolve_connector_config_organization_id,
    )
    monkeypatch.setattr(ingestion_service.repository, "get_connector_config", fake_get_connector_config)
    monkeypatch.setattr(ingestion_service.repository, "insert_ingestion_job", fake_insert_ingestion_job)
    monkeypatch.setattr(ingestion_service.repository, "update_ingestion_job", fake_update_ingestion_job)
    monkeypatch.setattr(
        ingestion_service.tenancy_service,
        "update_connector_sync_status",
        fake_update_connector_sync_status,
    )
    monkeypatch.setattr(ingestion_service, "set_tenant_context", fake_set_tenant_context)

    await ingestion_service._execute_ingestion_job(
        _FakeSession(), config_row.id, force_full_sync=True
    )

    # The stored value on the row is still the encrypted blob (never
    # mutated) -- but the connector was authenticated with the real
    # plaintext credential, decrypted exactly once for this job.
    assert config_row.credential_ref == encrypted_credential_ref
    assert fake_connector.received_credential_ref == plaintext_credential
    assert fake_connector.closed is True


@pytest.mark.asyncio
async def test_execute_ingestion_job_sets_tenant_context_before_reading_full_config(
    monkeypatch,
) -> None:
    """Milestone 10 RLS note: `_execute_ingestion_job` cannot call
    `set_tenant_context` until it knows the connector_config's organization
    id -- and it cannot know that until it resolves it via the RLS-bypassing
    `resolve_connector_config_organization_id` (since `connector_configs` is
    itself RLS-protected). This test asserts that exact ordering: resolve org
    id -> set_tenant_context -> read the full row.
    """
    organization_id = uuid.uuid4()
    encrypted_credential_ref = await encrypt_secret(get_kms(), "irrelevant-for-this-test")
    config_row = _FakeConnectorConfigRow(
        organization_id=organization_id,
        source="fake_source",
        credential_ref=encrypted_credential_ref,
    )
    fake_connector = _RecordingConnector()
    call_order: list[str] = []

    async def fake_resolve_connector_config_organization_id(session, connector_config_id):
        call_order.append("resolve_org_id")
        assert connector_config_id == config_row.id
        return organization_id

    async def fake_set_tenant_context(session, org_id) -> None:
        call_order.append("set_tenant_context")
        assert org_id == organization_id

    async def fake_get_connector_config(session, connector_config_id):
        call_order.append("get_connector_config")
        return config_row

    async def fake_insert_ingestion_job(session, *, organization_id, connector_config_id):
        return _FakeJobRow(organization_id, connector_config_id)

    async def fake_update_ingestion_job(session, job_id, **fields):
        row = _FakeJobRow(organization_id, config_row.id)
        row.id = job_id
        for key, value in fields.items():
            setattr(row, key, value)
        return row

    async def fake_update_connector_sync_status(session, actor, org_id, connector_config_id, **kwargs):
        return None

    monkeypatch.setitem(ingestion_service._CONNECTOR_REGISTRY, "fake_source", fake_connector)
    monkeypatch.setattr(
        ingestion_service.repository,
        "resolve_connector_config_organization_id",
        fake_resolve_connector_config_organization_id,
    )
    monkeypatch.setattr(ingestion_service.repository, "get_connector_config", fake_get_connector_config)
    monkeypatch.setattr(ingestion_service.repository, "insert_ingestion_job", fake_insert_ingestion_job)
    monkeypatch.setattr(ingestion_service.repository, "update_ingestion_job", fake_update_ingestion_job)
    monkeypatch.setattr(
        ingestion_service.tenancy_service,
        "update_connector_sync_status",
        fake_update_connector_sync_status,
    )
    monkeypatch.setattr(ingestion_service, "set_tenant_context", fake_set_tenant_context)

    await ingestion_service._execute_ingestion_job(
        _FakeSession(), config_row.id, force_full_sync=True
    )

    assert call_order == ["resolve_org_id", "set_tenant_context", "get_connector_config"]


class _FakeDocumentRow:
    def __init__(self, *, organization_id, source) -> None:
        self.id = uuid.uuid4()
        self.organization_id = organization_id
        self.source = source


@pytest.mark.asyncio
async def test_reindex_sets_tenant_context_before_reading_full_document(monkeypatch) -> None:
    """Milestone 10 RLS note: `reindex` has the identical chicken-and-egg
    shape as `_execute_ingestion_job` -- it starts from a bare `document_id`
    and `documents` is RLS-protected, so it must resolve the owning org via
    the RLS-bypassing lookup, `set_tenant_context`, and only then read the
    full row, before doing anything else.
    """
    organization_id = uuid.uuid4()
    document_row = _FakeDocumentRow(organization_id=organization_id, source="fake_source")
    connector_config_row = _FakeConnectorConfigRow(
        organization_id=organization_id,
        source="fake_source",
        credential_ref=await encrypt_secret(get_kms(), "irrelevant-for-this-test"),
    )
    fake_connector = _RecordingConnector()
    call_order: list[str] = []

    async def fake_resolve_document_organization_id(session, document_id):
        call_order.append("resolve_document_org_id")
        assert document_id == document_row.id
        return organization_id

    async def fake_set_tenant_context(session, org_id) -> None:
        call_order.append("set_tenant_context")
        assert org_id == organization_id

    async def fake_get_document_by_id(session, document_id):
        call_order.append("get_document_by_id")
        return document_row

    async def fake_get_connector_config_for_source(session, org_id, source):
        assert org_id == organization_id
        assert source == "fake_source"
        return connector_config_row

    async def fake_resolve_connector_config_organization_id(session, connector_config_id):
        return organization_id

    async def fake_get_connector_config(session, connector_config_id):
        return connector_config_row

    async def fake_insert_ingestion_job(session, *, organization_id, connector_config_id):
        return _FakeJobRow(organization_id, connector_config_id)

    async def fake_update_ingestion_job(session, job_id, **fields):
        row = _FakeJobRow(organization_id, connector_config_row.id)
        row.id = job_id
        for key, value in fields.items():
            setattr(row, key, value)
        return row

    async def fake_update_connector_sync_status(session, actor, org_id, connector_config_id, **kwargs):
        return None

    monkeypatch.setitem(ingestion_service._CONNECTOR_REGISTRY, "fake_source", fake_connector)
    monkeypatch.setattr(
        ingestion_service.repository,
        "resolve_document_organization_id",
        fake_resolve_document_organization_id,
    )
    monkeypatch.setattr(ingestion_service.repository, "get_document_by_id", fake_get_document_by_id)
    monkeypatch.setattr(
        ingestion_service.repository,
        "get_connector_config_for_source",
        fake_get_connector_config_for_source,
    )
    monkeypatch.setattr(
        ingestion_service.repository,
        "resolve_connector_config_organization_id",
        fake_resolve_connector_config_organization_id,
    )
    monkeypatch.setattr(ingestion_service.repository, "get_connector_config", fake_get_connector_config)
    monkeypatch.setattr(ingestion_service.repository, "insert_ingestion_job", fake_insert_ingestion_job)
    monkeypatch.setattr(ingestion_service.repository, "update_ingestion_job", fake_update_ingestion_job)
    monkeypatch.setattr(
        ingestion_service.tenancy_service,
        "update_connector_sync_status",
        fake_update_connector_sync_status,
    )
    monkeypatch.setattr(ingestion_service, "set_tenant_context", fake_set_tenant_context)

    await ingestion_service.reindex(_FakeSession(), document_row.id)

    # The document's own org must be resolved and set *before* the full
    # document row is read -- and that resolution happens before
    # _execute_ingestion_job's own (separate) connector_config resolution
    # runs, which is why only the first three entries are asserted here.
    assert call_order[:3] == [
        "resolve_document_org_id",
        "set_tenant_context",
        "get_document_by_id",
    ]


class _ResumeTokenConnector:
    """A fake connector with `supports_resume_token = True` -- records the
    `resume_token` it was actually called with, and returns a fixed one on
    its single page, so this test can assert both directions of the wiring
    `_execute_ingestion_job` owns: reading the persisted token in, and
    persisting whatever came back out.
    """

    source_name = "fake_resume_source"
    requests_per_second = 10.0
    supports_resume_token = True

    def __init__(self, *, resume_token_out: str | None) -> None:
        self.received_resume_token: str | None = "not called"
        self.resume_token_out = resume_token_out

    async def authenticate(self, config: ResolvedConnectorConfig):
        return object()

    async def fetch_batch(self, client, *, since, cursor, resume_token=None):
        self.received_resume_token = resume_token
        return FetchResult(items=[], next_cursor=None, has_more=False, resume_token=self.resume_token_out)

    def normalize(self, raw_item):  # pragma: no cover - never called in this test
        raise AssertionError("normalize should not be called with zero fetched items")

    async def close(self, client) -> None:
        return None


@pytest.mark.asyncio
async def test_execute_ingestion_job_reads_and_persists_resume_token(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    encrypted_credential_ref = await encrypt_secret(get_kms(), "irrelevant-for-this-test")
    config_row = _FakeConnectorConfigRow(
        organization_id=organization_id,
        source="fake_resume_source",
        credential_ref=encrypted_credential_ref,
    )
    config_row.config = {"_resume_token": '{"site-1": "https://example.com/old-delta"}'}
    fake_connector = _ResumeTokenConnector(
        resume_token_out='{"site-1": "https://example.com/new-delta"}'
    )
    captured_sync_status: dict[str, object] = {}

    async def fake_resolve_connector_config_organization_id(session, connector_config_id):
        return organization_id

    async def fake_get_connector_config(session, connector_config_id):
        return config_row

    async def fake_insert_ingestion_job(session, *, organization_id, connector_config_id):
        return _FakeJobRow(organization_id, connector_config_id)

    async def fake_update_ingestion_job(session, job_id, **fields):
        row = _FakeJobRow(organization_id, config_row.id)
        row.id = job_id
        for key, value in fields.items():
            setattr(row, key, value)
        return row

    async def fake_update_connector_sync_status(session, actor, org_id, connector_config_id, **kwargs):
        captured_sync_status.update(kwargs)
        return None

    async def fake_set_tenant_context(session, org_id) -> None:
        return None

    monkeypatch.setitem(ingestion_service._CONNECTOR_REGISTRY, "fake_resume_source", fake_connector)
    monkeypatch.setattr(
        ingestion_service.repository,
        "resolve_connector_config_organization_id",
        fake_resolve_connector_config_organization_id,
    )
    monkeypatch.setattr(ingestion_service.repository, "get_connector_config", fake_get_connector_config)
    monkeypatch.setattr(ingestion_service.repository, "insert_ingestion_job", fake_insert_ingestion_job)
    monkeypatch.setattr(ingestion_service.repository, "update_ingestion_job", fake_update_ingestion_job)
    monkeypatch.setattr(
        ingestion_service.tenancy_service,
        "update_connector_sync_status",
        fake_update_connector_sync_status,
    )
    monkeypatch.setattr(ingestion_service, "set_tenant_context", fake_set_tenant_context)

    await ingestion_service._execute_ingestion_job(
        _FakeSession(), config_row.id, force_full_sync=True
    )

    # The persisted token from *last* sync was read and handed to
    # fetch_batch...
    assert fake_connector.received_resume_token == '{"site-1": "https://example.com/old-delta"}'
    # ...and whatever fetch_batch returned this time is what gets persisted
    # for *next* time.
    assert captured_sync_status["config_patch"] == {
        "_resume_token": '{"site-1": "https://example.com/new-delta"}'
    }


@pytest.mark.asyncio
async def test_execute_ingestion_job_ignores_resume_token_for_unsupporting_connector(
    monkeypatch,
) -> None:
    """`_RecordingConnector` (used throughout this file) declares no
    `supports_resume_token` attribute at all -- confirms `getattr(...,
    False)` is used, not direct attribute access, so every connector
    written before this feature existed keeps working unchanged.
    """
    organization_id = uuid.uuid4()
    encrypted_credential_ref = await encrypt_secret(get_kms(), "irrelevant-for-this-test")
    config_row = _FakeConnectorConfigRow(
        organization_id=organization_id,
        source="fake_source",
        credential_ref=encrypted_credential_ref,
    )
    fake_connector = _RecordingConnector()

    async def fake_resolve_connector_config_organization_id(session, connector_config_id):
        return organization_id

    async def fake_get_connector_config(session, connector_config_id):
        return config_row

    async def fake_insert_ingestion_job(session, *, organization_id, connector_config_id):
        return _FakeJobRow(organization_id, connector_config_id)

    async def fake_update_ingestion_job(session, job_id, **fields):
        row = _FakeJobRow(organization_id, config_row.id)
        row.id = job_id
        for key, value in fields.items():
            setattr(row, key, value)
        return row

    async def fake_update_connector_sync_status(session, actor, org_id, connector_config_id, **kwargs):
        return None

    async def fake_set_tenant_context(session, org_id) -> None:
        return None

    monkeypatch.setitem(ingestion_service._CONNECTOR_REGISTRY, "fake_source", fake_connector)
    monkeypatch.setattr(
        ingestion_service.repository,
        "resolve_connector_config_organization_id",
        fake_resolve_connector_config_organization_id,
    )
    monkeypatch.setattr(ingestion_service.repository, "get_connector_config", fake_get_connector_config)
    monkeypatch.setattr(ingestion_service.repository, "insert_ingestion_job", fake_insert_ingestion_job)
    monkeypatch.setattr(ingestion_service.repository, "update_ingestion_job", fake_update_ingestion_job)
    monkeypatch.setattr(
        ingestion_service.tenancy_service,
        "update_connector_sync_status",
        fake_update_connector_sync_status,
    )
    monkeypatch.setattr(ingestion_service, "set_tenant_context", fake_set_tenant_context)

    # Must not raise -- `_RecordingConnector` has no `supports_resume_token`
    # attribute, `fetch_batch` accepts no `resume_token` kwarg either.
    await ingestion_service._execute_ingestion_job(
        _FakeSession(), config_row.id, force_full_sync=True
    )


class _ExplodingConnector(_RecordingConnector):
    """Authenticates fine, then fails inside the fetch loop -- the shape of
    a real mid-sync failure (API 500, expired token, rate-limit rejection).
    """

    source_name = "exploding_source"

    async def fetch_batch(self, client, *, since, cursor):
        raise RuntimeError("simulated upstream API failure during fetch")


@pytest.mark.asyncio
async def test_failed_ingestion_job_leaves_a_durable_failure_record(monkeypatch) -> None:
    """Regression test for a real production bug: a failed job used to erase
    its own failure record.

    `_execute_ingestion_job` writes `status="failed"` and then used to
    re-raise. The whole call runs inside the single transaction the
    caller's `session_scope()` opened, and that helper's contract is
    "commit on normal return, roll back on any escaping exception" -- so the
    re-raise rolled back the very `status="failed"` write (and the original
    `insert_ingestion_job` row with it). Every failed ingestion job left
    ZERO trace in `ingestion_jobs`: operators saw no failed jobs, not
    because none failed, but because each one deleted its own evidence.

    The fix is that the failure path returns normally instead of raising.
    This test therefore asserts BOTH halves:
      - the failure is fully recorded (status/stage/timestamp/job id), and
      - nothing propagates out, so `session_scope()` reaches its commit.
    """
    organization_id = uuid.uuid4()
    config_row = _FakeConnectorConfigRow(
        organization_id=organization_id,
        source="exploding_source",
        credential_ref=await encrypt_secret(get_kms(), "irrelevant-for-this-test"),
    )
    fake_connector = _ExplodingConnector()
    job_updates: list[dict] = []
    sync_status_updates: list[dict] = []
    inserted_job_id: uuid.UUID | None = None

    async def fake_resolve_connector_config_organization_id(session, connector_config_id):
        return organization_id

    async def fake_get_connector_config(session, connector_config_id):
        return config_row

    async def fake_insert_ingestion_job(session, *, organization_id, connector_config_id):
        nonlocal inserted_job_id
        row = _FakeJobRow(organization_id, connector_config_id)
        inserted_job_id = row.id
        return row

    async def fake_update_ingestion_job(session, job_id, **fields):
        job_updates.append({"job_id": job_id, **fields})
        row = _FakeJobRow(organization_id, config_row.id)
        row.id = job_id
        for key, value in fields.items():
            setattr(row, key, value)
        return row

    async def fake_update_connector_sync_status(session, actor, org_id, connector_config_id, **kwargs):
        sync_status_updates.append(kwargs)
        return None

    async def fake_set_tenant_context(session, org_id) -> None:
        return None

    monkeypatch.setitem(ingestion_service._CONNECTOR_REGISTRY, "exploding_source", fake_connector)
    monkeypatch.setattr(
        ingestion_service.repository,
        "resolve_connector_config_organization_id",
        fake_resolve_connector_config_organization_id,
    )
    monkeypatch.setattr(ingestion_service.repository, "get_connector_config", fake_get_connector_config)
    monkeypatch.setattr(ingestion_service.repository, "insert_ingestion_job", fake_insert_ingestion_job)
    monkeypatch.setattr(ingestion_service.repository, "update_ingestion_job", fake_update_ingestion_job)
    monkeypatch.setattr(
        ingestion_service.tenancy_service,
        "update_connector_sync_status",
        fake_update_connector_sync_status,
    )
    monkeypatch.setattr(ingestion_service, "set_tenant_context", fake_set_tenant_context)

    # Must NOT raise -- an escaping exception is exactly what used to reach
    # `session_scope()` and roll the failure record back.
    job = await ingestion_service._execute_ingestion_job(
        _FakeSession(), config_row.id, force_full_sync=True
    )

    assert job.status == "failed"
    assert job.failed_stage == "fetch", "the stage that actually failed must be recorded"
    assert job.id == inserted_job_id, "the failure must be recorded against the original job row"
    assert job.completed_at is not None, "a failed job must carry a completion timestamp"

    failure_writes = [u for u in job_updates if u.get("status") == "failed"]
    assert len(failure_writes) == 1, f"expected exactly one failure write, got {job_updates}"
    assert failure_writes[0]["job_id"] == inserted_job_id

    assert sync_status_updates and sync_status_updates[-1]["status"] == "error", (
        "the connector itself must be marked errored so operators can see it in tenancy"
    )
    assert fake_connector.closed is True, "the connector client must still be closed on failure"


@pytest.mark.asyncio
async def test_failed_ingestion_job_does_not_report_documents_processed(monkeypatch) -> None:
    """The savepoint rolls back every document written by the failed attempt,
    so reporting a non-zero count would claim documents that no longer
    exist.
    """
    organization_id = uuid.uuid4()
    config_row = _FakeConnectorConfigRow(
        organization_id=organization_id,
        source="exploding_source",
        credential_ref=await encrypt_secret(get_kms(), "irrelevant-for-this-test"),
    )
    job_updates: list[dict] = []

    async def fake_update_ingestion_job(session, job_id, **fields):
        job_updates.append(fields)
        row = _FakeJobRow(organization_id, config_row.id)
        row.id = job_id
        for key, value in fields.items():
            setattr(row, key, value)
        return row

    async def _noop_sync_status(session, actor, org_id, connector_config_id, **kwargs):
        return None

    monkeypatch.setitem(ingestion_service._CONNECTOR_REGISTRY, "exploding_source", _ExplodingConnector())
    monkeypatch.setattr(
        ingestion_service.repository,
        "resolve_connector_config_organization_id",
        lambda session, cid: _async_return(organization_id),
    )
    monkeypatch.setattr(
        ingestion_service.repository, "get_connector_config", lambda s, c: _async_return(config_row)
    )
    monkeypatch.setattr(
        ingestion_service.repository,
        "insert_ingestion_job",
        lambda session, *, organization_id, connector_config_id: _async_return(
            _FakeJobRow(organization_id, connector_config_id)
        ),
    )
    monkeypatch.setattr(ingestion_service.repository, "update_ingestion_job", fake_update_ingestion_job)
    monkeypatch.setattr(
        ingestion_service.tenancy_service, "update_connector_sync_status", _noop_sync_status
    )
    monkeypatch.setattr(
        ingestion_service, "set_tenant_context", lambda session, org: _async_return(None)
    )

    await ingestion_service._execute_ingestion_job(
        _FakeSession(), config_row.id, force_full_sync=True
    )

    failure_writes = [u for u in job_updates if u.get("status") == "failed"]
    assert failure_writes, "no failure write recorded"
    assert "documents_processed" not in failure_writes[0]


async def _async_return(value):
    return value


@pytest.mark.asyncio
async def test_execute_ingestion_job_acquires_both_rate_limit_buckets(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    encrypted_credential_ref = await encrypt_secret(get_kms(), "irrelevant-for-this-test")

    config_row = _FakeConnectorConfigRow(
        organization_id=organization_id,
        source="fake_source",
        credential_ref=encrypted_credential_ref,
    )
    fake_connector = _RecordingConnector()
    acquired_keys: list[tuple[str, float]] = []

    async def fake_acquire(key: str, rate: float) -> None:
        acquired_keys.append((key, rate))

    async def fake_resolve_connector_config_organization_id(session, connector_config_id):
        return organization_id

    async def fake_get_connector_config(session, connector_config_id):
        return config_row

    async def fake_insert_ingestion_job(session, *, organization_id, connector_config_id):
        return _FakeJobRow(organization_id, connector_config_id)

    async def fake_update_ingestion_job(session, job_id, **fields):
        row = _FakeJobRow(organization_id, config_row.id)
        row.id = job_id
        for key, value in fields.items():
            setattr(row, key, value)
        return row

    async def fake_update_connector_sync_status(session, actor, org_id, connector_config_id, **kwargs):
        return None

    async def fake_set_tenant_context(session, org_id) -> None:
        return None

    monkeypatch.setitem(ingestion_service._CONNECTOR_REGISTRY, "fake_source", fake_connector)
    monkeypatch.setattr(
        ingestion_service.repository,
        "resolve_connector_config_organization_id",
        fake_resolve_connector_config_organization_id,
    )
    monkeypatch.setattr(ingestion_service.repository, "get_connector_config", fake_get_connector_config)
    monkeypatch.setattr(ingestion_service.repository, "insert_ingestion_job", fake_insert_ingestion_job)
    monkeypatch.setattr(ingestion_service.repository, "update_ingestion_job", fake_update_ingestion_job)
    monkeypatch.setattr(
        ingestion_service.tenancy_service,
        "update_connector_sync_status",
        fake_update_connector_sync_status,
    )
    monkeypatch.setattr(ingestion_service, "set_tenant_context", fake_set_tenant_context)
    monkeypatch.setattr(ingestion_service._rate_limiter, "acquire", fake_acquire)

    await ingestion_service._execute_ingestion_job(
        _FakeSession(), config_row.id, force_full_sync=True
    )

    keys = [key for key, _rate in acquired_keys]
    assert f"connector:{config_row.id}" in keys
    assert f"org:{organization_id}" in keys
    connector_rate = dict(acquired_keys)[f"connector:{config_row.id}"]
    assert connector_rate == fake_connector.requests_per_second
