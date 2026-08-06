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
    encrypted_credential_ref = encrypt_secret(get_kms(), plaintext_credential)

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
    encrypted_credential_ref = encrypt_secret(get_kms(), "irrelevant-for-this-test")
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
        credential_ref=encrypt_secret(get_kms(), "irrelevant-for-this-test"),
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


@pytest.mark.asyncio
async def test_execute_ingestion_job_acquires_both_rate_limit_buckets(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    encrypted_credential_ref = encrypt_secret(get_kms(), "irrelevant-for-this-test")

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
