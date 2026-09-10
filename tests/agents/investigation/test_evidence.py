"""Tests for `app.agents.investigation.evidence`:
  - `_chunk_to_evidence`'s kind -> source mapping -- the piece that lets the
    Investigation Agent tell a GitHub file chunk apart from a commit/
    pull-request/issue chunk, now that `retrieval.search(...,
    include_metadata=True)` can surface each chunk's `document_metadata`
    (see `ingestion.connectors.github`'s module docstring for the `"kind"`
    metadata key convention this relies on).
  - `_should_augment_with_live_evidence`'s hybrid trigger logic and
    `_gather_live_evidence`'s dispatch-by-connector-source behavior -- the
    live-evidence extension (`agents.investigation.live/`).
  - `_gather_postmortem_evidence`'s P4 deduplication against runbook
    evidence, and `_is_duplicate_of_runbook_evidence`'s correlation-key/
    content-identity-fallback logic it relies on.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.agents.investigation import evidence as evidence_module
from app.agents.investigation.evidence import (
    _LIVE_SOURCES,
    _chunk_to_evidence,
    _gather_knowledge_evidence,
    _gather_live_evidence,
    _gather_postmortem_evidence,
    _is_duplicate_of_runbook_evidence,
    _parse_source_timestamp,
    _should_augment_with_live_evidence,
)
from app.agents.investigation.live.monitoring_live import MonitoringLiveSource
from app.core.incidents.schemas import Postmortem
from app.core.tenancy.schemas import ConnectorConfig
from app.retrieval.schemas import ScoredChunk, SearchFilters
from app.shared.schemas import EvidenceItem, Identity


def _chunk(metadata: dict[str, str], content: str = "some content") -> ScoredChunk:
    return ScoredChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        collection="code",
        content=content,
        score=0.9,
        source_offset_start=0,
        source_offset_end=len(content),
        title="a title",
        source_url="https://github.com/acme/widgets/commit/abc123",
        metadata=metadata,
    )


def test_file_chunk_with_no_kind_defaults_to_github() -> None:
    chunk = _chunk({"repo": "acme/widgets", "path": "src/app.py", "ref": "main"})

    evidence = _chunk_to_evidence(chunk)

    assert evidence.source == "github"
    assert evidence.metadata == chunk.metadata
    assert evidence.source_timestamp is None  # no "timestamp" key on a file chunk


def test_commit_chunk_maps_to_commit_source() -> None:
    chunk = _chunk(
        {
            "repo": "acme/widgets",
            "kind": "commit",
            "sha": "abc123",
            "author": "Ada Lovelace",
            "timestamp": "2026-07-01T10:00:00Z",
            "changed_files": "src/checkout.py",
        }
    )

    evidence = _chunk_to_evidence(chunk)

    assert evidence.source == "commit"
    assert evidence.metadata["sha"] == "abc123"
    assert evidence.source_timestamp == datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)


def test_pull_request_chunk_maps_to_pull_request_source() -> None:
    chunk = _chunk({"repo": "acme/widgets", "kind": "pull_request", "number": "42"})

    evidence = _chunk_to_evidence(chunk)

    assert evidence.source == "pull_request"


def test_issue_chunk_maps_to_issue_source() -> None:
    chunk = _chunk({"repo": "acme/widgets", "kind": "issue", "number": "7", "labels": "bug"})

    evidence = _chunk_to_evidence(chunk)

    assert evidence.source == "issue"
    assert evidence.metadata["labels"] == "bug"


def test_explicit_source_override_wins_over_kind_metadata() -> None:
    """`_gather_slack_evidence` always passes `source="slack"` explicitly --
    confirms that override takes priority even if `metadata["kind"]` were
    somehow present (it never is for real Slack chunks).
    """
    chunk = _chunk({"kind": "commit"})

    evidence = _chunk_to_evidence(chunk, source="slack")

    assert evidence.source == "slack"


def test_reference_falls_back_to_chunk_id_without_source_url() -> None:
    chunk = _chunk({})
    chunk = chunk.model_copy(update={"source_url": None})

    evidence = _chunk_to_evidence(chunk)

    assert evidence.reference == f"chunk:{chunk.chunk_id}"


def test_summary_is_truncated_and_marked_with_ellipsis() -> None:
    long_content = "x" * 500
    chunk = _chunk({}, content=long_content)

    evidence = _chunk_to_evidence(chunk)

    assert len(evidence.summary) == 303  # 300 chars + "..."
    assert evidence.summary.endswith("...")


def test_parse_source_timestamp_handles_missing_and_malformed_values() -> None:
    assert _parse_source_timestamp(None) is None
    assert _parse_source_timestamp("") is None
    assert _parse_source_timestamp("not-a-timestamp") is None
    assert _parse_source_timestamp("2026-07-01T10:00:00Z") == datetime(
        2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc
    )


# --- _gather_knowledge_evidence ---------------------------------------------


def _knowledge_chunk(metadata: dict[str, str] | None = None, content: str = "runbook content") -> ScoredChunk:
    return ScoredChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        collection="documentation",
        content=content,
        score=0.8,
        source_offset_start=0,
        source_offset_end=len(content),
        title="Runbook title",
        source_url="https://example.com/knowledge/doc-1",
        metadata=metadata or {},
    )


@pytest.mark.asyncio
async def test_gather_knowledge_evidence_excludes_github_tagged_chunks(monkeypatch) -> None:
    """The complementary filter to `_gather_code_evidence`'s: a chunk with
    `repo` metadata is GitHub-sourced and belongs to that source instead,
    not this one -- otherwise the same "documentation" collection chunk
    could be double-counted across both sources.
    """
    github_chunk = _knowledge_chunk({"repo": "acme/widgets"})
    runbook_chunk = _knowledge_chunk({})

    async def fake_search(session, query, filters, top_k, collection=None, *, include_metadata=False):
        assert collection == "documentation"
        assert include_metadata is True
        return [github_chunk, runbook_chunk]

    monkeypatch.setattr(evidence_module.retrieval_service, "search", fake_search)

    result = await _gather_knowledge_evidence(
        session=None,
        query="checkout failing",
        filters=SearchFilters(organization_id=uuid.uuid4()),
        retry_count={},
    )

    assert len(result) == 1
    assert result[0].reference == runbook_chunk.source_url


@pytest.mark.asyncio
async def test_gather_knowledge_evidence_returns_non_github_documentation_chunks_as_runbook_source(
    monkeypatch,
) -> None:
    manual_chunk = _knowledge_chunk({}, content="Restart the payment worker.")

    async def fake_search(session, query, filters, top_k, collection=None, *, include_metadata=False):
        return [manual_chunk]

    monkeypatch.setattr(evidence_module.retrieval_service, "search", fake_search)

    result = await _gather_knowledge_evidence(
        session=None,
        query="payment worker stuck",
        filters=SearchFilters(organization_id=uuid.uuid4()),
        retry_count={},
    )

    assert len(result) == 1
    assert result[0].source == "runbook"
    assert result[0].summary.startswith("Restart the payment worker.")


@pytest.mark.asyncio
async def test_gather_knowledge_evidence_logs_and_skips_on_failure(monkeypatch) -> None:
    import app.agents.retry as retry_module

    async def instant_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(retry_module.asyncio, "sleep", instant_sleep)

    async def failing_search(session, query, filters, top_k, collection=None, *, include_metadata=False):
        raise RuntimeError("retrieval unavailable")

    monkeypatch.setattr(evidence_module.retrieval_service, "search", failing_search)

    result = await _gather_knowledge_evidence(
        session=None,
        query="q",
        filters=SearchFilters(organization_id=uuid.uuid4()),
        retry_count={},
    )

    assert result == []


@pytest.mark.asyncio
async def test_gather_evidence_includes_knowledge_source_in_priority_order(monkeypatch) -> None:
    """End-to-end: `gather_evidence` calls `_gather_knowledge_evidence`
    between code and Slack evidence (module docstring's priority order,
    item 2), and its results (source="runbook") reach the final list.
    """
    from app.agents.investigation.evidence import gather_evidence

    async def fake_code(session, query, filters, retry_count):
        return []

    async def fake_slack(session, query, filters, retry_count):
        return []

    knowledge_item = EvidenceItem(
        source="runbook", reference="chunk:1", summary="restart the worker", retrieved_at=datetime.now(timezone.utc)
    )

    async def fake_knowledge(session, query, filters, retry_count):
        return [knowledge_item]

    async def fake_postmortem(session, actor, retry_count, existing_evidence):
        return []

    monkeypatch.setattr(evidence_module, "_gather_code_evidence", fake_code)
    monkeypatch.setattr(evidence_module, "_gather_knowledge_evidence", fake_knowledge)
    monkeypatch.setattr(evidence_module, "_gather_slack_evidence", fake_slack)
    monkeypatch.setattr(evidence_module, "_gather_postmortem_evidence", fake_postmortem)
    monkeypatch.setattr(evidence_module, "_gather_jira_evidence", lambda: [])
    monkeypatch.setattr(evidence_module, "_gather_monitoring_evidence", lambda: [])
    monkeypatch.setattr(evidence_module.get_settings(), "investigation_live_evidence_enabled", False)

    actor = Identity.for_agent("test_agent", uuid.uuid4())
    result = await gather_evidence(session=None, query="worker stuck", actor=actor, retry_count={})

    assert knowledge_item in result


# --- _gather_postmortem_evidence / P4 deduplication -------------------------


def _postmortem(
    incident_id: uuid.UUID | None = None, root_cause: str = "Cache stampede on checkout"
) -> Postmortem:
    now = datetime.now(timezone.utc)
    return Postmortem(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        incident_id=incident_id or uuid.uuid4(),
        status="approved",
        root_cause=root_cause,
        action_items=[],
        generated_by="agent",
        reviewed_by=None,
        created_at=now,
        updated_at=now,
    )


def _runbook_evidence(
    metadata: dict[str, str] | None = None, summary: str = "Restart the payment worker."
) -> EvidenceItem:
    return EvidenceItem(
        source="runbook",
        reference="chunk:1",
        summary=summary,
        retrieved_at=datetime.now(timezone.utc),
        metadata=metadata or {},
    )


@pytest.mark.asyncio
async def test_gather_postmortem_evidence_deduplicates_by_incident_id(monkeypatch) -> None:
    """Req 1: the same postmortem represented as both postmortem evidence
    and runbook evidence (correlated via `incident_id`) appears only once
    -- the runbook copy is kept, the redundant postmortem item is dropped.
    """
    incident_id = uuid.uuid4()
    postmortem = _postmortem(incident_id=incident_id)

    async def fake_list_recent(session, actor, organization_id, limit):
        return [postmortem]

    monkeypatch.setattr(evidence_module.incidents_service, "list_recent_postmortems", fake_list_recent)

    runbook_item = _runbook_evidence(metadata={"incident_id": str(incident_id)})

    result = await _gather_postmortem_evidence(
        session=None,
        actor=Identity.for_agent("test_agent", uuid.uuid4()),
        retry_count={},
        existing_evidence=[runbook_item],
    )

    assert result == []


@pytest.mark.asyncio
async def test_gather_postmortem_evidence_preserves_distinct_postmortems(monkeypatch) -> None:
    """Req 2: postmortems whose incident is not represented in runbook
    evidence at all are unaffected by dedup -- both are kept.
    """
    postmortem_a = _postmortem()
    postmortem_b = _postmortem()

    async def fake_list_recent(session, actor, organization_id, limit):
        return [postmortem_a, postmortem_b]

    monkeypatch.setattr(evidence_module.incidents_service, "list_recent_postmortems", fake_list_recent)

    result = await _gather_postmortem_evidence(
        session=None,
        actor=Identity.for_agent("test_agent", uuid.uuid4()),
        retry_count={},
        existing_evidence=[_runbook_evidence(metadata={"incident_id": str(uuid.uuid4())})],
    )

    assert {item.reference for item in result} == {
        f"postmortem:{postmortem_a.id}",
        f"postmortem:{postmortem_b.id}",
    }


@pytest.mark.asyncio
async def test_gather_evidence_preserves_unrelated_runbook_evidence(monkeypatch) -> None:
    """Req 3: a runbook evidence item unrelated to any gathered postmortem
    stays in the final evidence list untouched -- dedup only ever removes
    postmortem items, never runbook ones.
    """
    from app.agents.investigation.evidence import gather_evidence

    unrelated_runbook = _runbook_evidence(
        metadata={"incident_id": str(uuid.uuid4())}, summary="Restart the payment worker."
    )
    postmortem = _postmortem()  # unrelated incident_id, no overlap

    async def fake_code(session, query, filters, retry_count):
        return []

    async def fake_knowledge(session, query, filters, retry_count):
        return [unrelated_runbook]

    async def fake_slack(session, query, filters, retry_count):
        return []

    async def fake_list_recent(session, actor, organization_id, limit):
        return [postmortem]

    monkeypatch.setattr(evidence_module, "_gather_code_evidence", fake_code)
    monkeypatch.setattr(evidence_module, "_gather_knowledge_evidence", fake_knowledge)
    monkeypatch.setattr(evidence_module, "_gather_slack_evidence", fake_slack)
    monkeypatch.setattr(evidence_module, "_gather_jira_evidence", lambda: [])
    monkeypatch.setattr(evidence_module, "_gather_monitoring_evidence", lambda: [])
    monkeypatch.setattr(evidence_module.incidents_service, "list_recent_postmortems", fake_list_recent)
    monkeypatch.setattr(evidence_module.get_settings(), "investigation_live_evidence_enabled", False)

    actor = Identity.for_agent("test_agent", uuid.uuid4())
    result = await gather_evidence(session=None, query="worker stuck", actor=actor, retry_count={})

    assert unrelated_runbook in result
    assert any(item.reference == f"postmortem:{postmortem.id}" for item in result)


@pytest.mark.asyncio
async def test_gather_evidence_dedup_does_not_remove_github_or_slack_evidence(monkeypatch) -> None:
    """Req 4: deduplication is scoped to postmortem-vs-runbook only -- a
    duplicate postmortem being dropped never touches GitHub/Slack (or any
    other source's) evidence already gathered.
    """
    from app.agents.investigation.evidence import gather_evidence

    incident_id = uuid.uuid4()
    postmortem = _postmortem(incident_id=incident_id)
    runbook_item = _runbook_evidence(metadata={"incident_id": str(incident_id)})
    github_item = EvidenceItem(
        source="github", reference="ref-1", summary="a file", retrieved_at=datetime.now(timezone.utc)
    )
    slack_item = EvidenceItem(
        source="slack", reference="ref-2", summary="a message", retrieved_at=datetime.now(timezone.utc)
    )

    async def fake_code(session, query, filters, retry_count):
        return [github_item]

    async def fake_knowledge(session, query, filters, retry_count):
        return [runbook_item]

    async def fake_slack(session, query, filters, retry_count):
        return [slack_item]

    async def fake_list_recent(session, actor, organization_id, limit):
        return [postmortem]

    monkeypatch.setattr(evidence_module, "_gather_code_evidence", fake_code)
    monkeypatch.setattr(evidence_module, "_gather_knowledge_evidence", fake_knowledge)
    monkeypatch.setattr(evidence_module, "_gather_slack_evidence", fake_slack)
    monkeypatch.setattr(evidence_module, "_gather_jira_evidence", lambda: [])
    monkeypatch.setattr(evidence_module, "_gather_monitoring_evidence", lambda: [])
    monkeypatch.setattr(evidence_module.incidents_service, "list_recent_postmortems", fake_list_recent)
    monkeypatch.setattr(evidence_module.get_settings(), "investigation_live_evidence_enabled", False)

    actor = Identity.for_agent("test_agent", uuid.uuid4())
    result = await gather_evidence(session=None, query="checkout failing", actor=actor, retry_count={})

    assert github_item in result
    assert slack_item in result
    assert runbook_item in result
    assert not any(item.reference == f"postmortem:{postmortem.id}" for item in result)


@pytest.mark.asyncio
async def test_gather_evidence_priority_order_unchanged_when_no_duplicate(monkeypatch) -> None:
    """Req 5: when no duplicate exists, the final evidence list's order is
    exactly the pre-P4 priority order (code, knowledge/runbook, slack,
    postmortem) -- dedup only ever removes items, never reorders survivors.
    """
    from app.agents.investigation.evidence import gather_evidence

    github_item = EvidenceItem(
        source="github", reference="ref-1", summary="a file", retrieved_at=datetime.now(timezone.utc)
    )
    runbook_item = _runbook_evidence(metadata={"incident_id": str(uuid.uuid4())})
    slack_item = EvidenceItem(
        source="slack", reference="ref-2", summary="a message", retrieved_at=datetime.now(timezone.utc)
    )
    postmortem = _postmortem()  # distinct incident_id, no overlap

    async def fake_code(session, query, filters, retry_count):
        return [github_item]

    async def fake_knowledge(session, query, filters, retry_count):
        return [runbook_item]

    async def fake_slack(session, query, filters, retry_count):
        return [slack_item]

    async def fake_list_recent(session, actor, organization_id, limit):
        return [postmortem]

    monkeypatch.setattr(evidence_module, "_gather_code_evidence", fake_code)
    monkeypatch.setattr(evidence_module, "_gather_knowledge_evidence", fake_knowledge)
    monkeypatch.setattr(evidence_module, "_gather_slack_evidence", fake_slack)
    monkeypatch.setattr(evidence_module, "_gather_jira_evidence", lambda: [])
    monkeypatch.setattr(evidence_module, "_gather_monitoring_evidence", lambda: [])
    monkeypatch.setattr(evidence_module.incidents_service, "list_recent_postmortems", fake_list_recent)
    monkeypatch.setattr(evidence_module.get_settings(), "investigation_live_evidence_enabled", False)

    actor = Identity.for_agent("test_agent", uuid.uuid4())
    result = await gather_evidence(session=None, query="checkout failing", actor=actor, retry_count={})

    assert [item.source for item in result] == ["github", "runbook", "slack", "postmortem"]


def test_is_duplicate_of_runbook_evidence_fallback_matches_on_content_identity() -> None:
    """Positive fallback case: no runbook evidence item carries an
    `incident_id` key at all (e.g. a P3 manually-published document), so
    correlation falls back to content identity -- the postmortem's own
    root-cause text appearing verbatim in the runbook item's summary is
    treated as the same underlying knowledge.
    """
    postmortem = _postmortem(root_cause="Cache stampede on checkout")
    runbook_item = _runbook_evidence(metadata={}, summary="Root cause: Cache stampede on checkout")

    assert _is_duplicate_of_runbook_evidence(
        postmortem, postmortem.root_cause, [runbook_item], correlated_incident_ids=set()
    )


def test_is_duplicate_of_runbook_evidence_missing_correlation_metadata_does_not_remove_unrelated() -> None:
    """Req 6: a runbook evidence item with no `incident_id` key (so the
    correlation key is unavailable) must not cause an *unrelated*
    postmortem to be incorrectly matched -- the content-identity fallback
    requires the postmortem's own summary text to actually appear in that
    item, not just "some runbook evidence exists with no key."
    """
    postmortem = _postmortem(root_cause="Cache stampede on checkout")
    unrelated_runbook_item = _runbook_evidence(metadata={}, summary="Restart the payment worker.")

    assert not _is_duplicate_of_runbook_evidence(
        postmortem, postmortem.root_cause, [unrelated_runbook_item], correlated_incident_ids=set()
    )


# --- _should_augment_with_live_evidence -------------------------------------


def _evidence_item(source_timestamp: datetime | None = None) -> EvidenceItem:
    return EvidenceItem(
        source="github",
        reference="ref",
        summary="summary",
        retrieved_at=datetime.now(timezone.utc),
        source_timestamp=source_timestamp,
    )


def test_should_augment_when_incident_id_present() -> None:
    now = datetime.now(timezone.utc)
    evidence = [_evidence_item(source_timestamp=now) for _ in range(10)]  # plenty, fresh

    assert _should_augment_with_live_evidence(evidence, uuid.uuid4()) is True


def test_should_augment_when_evidence_is_thin() -> None:
    now = datetime.now(timezone.utc)
    evidence = [_evidence_item(source_timestamp=now)]  # below _LIVE_EVIDENCE_MIN_COUNT

    assert _should_augment_with_live_evidence(evidence, None) is True


def test_should_augment_when_evidence_is_stale() -> None:
    stale = datetime.now(timezone.utc) - timedelta(hours=5)
    evidence = [_evidence_item(source_timestamp=stale) for _ in range(5)]

    assert _should_augment_with_live_evidence(evidence, None) is True


def test_should_augment_when_no_evidence_is_timestamped() -> None:
    evidence = [_evidence_item(source_timestamp=None) for _ in range(5)]

    assert _should_augment_with_live_evidence(evidence, None) is True


def test_should_not_augment_when_plenty_of_fresh_evidence_and_no_incident() -> None:
    now = datetime.now(timezone.utc)
    evidence = [_evidence_item(source_timestamp=now) for _ in range(5)]

    assert _should_augment_with_live_evidence(evidence, None) is False


# --- _gather_live_evidence ---------------------------------------------------


def _connector_config(source: str, status: str = "active") -> ConnectorConfig:
    now = datetime.now(timezone.utc)
    return ConnectorConfig(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        project_id=None,
        source=source,
        credential_ref="token-ref",
        config={},
        status=status,
        last_synced_at=None,
        created_at=now,
        updated_at=now,
    )


class _FakeLiveSource:
    def __init__(self, items: list[EvidenceItem]) -> None:
        self._items = items
        self.calls = 0

    async def fetch_live_evidence(self, *, connector_config, query, since, limit):
        self.calls += 1
        return self._items


def test_monitoring_connector_source_resolves_to_monitoring_live_source() -> None:
    """`ConnectorSource.MONITORING`'s whole point: a `connector_config` with
    `source="monitoring"` must actually dispatch to `MonitoringLiveSource`,
    not be silently skipped the way an unregistered source (e.g. `"jira"`)
    already is (`test_gather_live_evidence_dispatches_by_connector_source`
    covers that skip behavior). Uses the real, module-level `_LIVE_SOURCES`
    (not a monkeypatched fake, unlike the dispatch tests below) so this test
    fails if the registration itself is ever accidentally removed.
    """
    assert "monitoring" in _LIVE_SOURCES
    assert isinstance(_LIVE_SOURCES["monitoring"], MonitoringLiveSource)


@pytest.mark.asyncio
async def test_gather_live_evidence_dispatches_monitoring_connector(monkeypatch) -> None:
    """End-to-end confirmation that a `source="monitoring"` connector config
    is no longer silently skipped: `_gather_live_evidence` now looks it up in
    `_LIVE_SOURCES` and calls its `fetch_live_evidence`, exactly like
    `"github"`/`"slack"` already do.
    """
    monitoring_config = _connector_config("monitoring")

    async def fake_list_connectors(session, actor, organization_id):
        return [monitoring_config]

    monkeypatch.setattr(evidence_module.tenancy_service, "list_connectors", fake_list_connectors)

    monitoring_item = EvidenceItem(
        source="monitoring", reference="r3", summary="s3", retrieved_at=datetime.now(timezone.utc)
    )
    fake_monitoring = _FakeLiveSource([monitoring_item])
    monkeypatch.setattr(evidence_module, "_LIVE_SOURCES", {"monitoring": fake_monitoring})

    actor = Identity.for_agent("test_agent", uuid.uuid4())
    result = await _gather_live_evidence(
        session=None, query="disk usage spike", actor=actor, retry_count={}
    )

    assert result == [monitoring_item]
    assert fake_monitoring.calls == 1


@pytest.mark.asyncio
async def test_gather_live_evidence_dispatches_by_connector_source(monkeypatch) -> None:
    """One live source per connector `source`; a connector whose source has
    no registered `LiveEvidenceSource` (here, `"jira"`) is silently skipped
    -- mirroring this module's existing, honestly-empty `_gather_jira_evidence`.
    """
    github_config = _connector_config("github")
    slack_config = _connector_config("slack")
    jira_config = _connector_config("jira")

    async def fake_list_connectors(session, actor, organization_id):
        return [github_config, slack_config, jira_config]

    monkeypatch.setattr(evidence_module.tenancy_service, "list_connectors", fake_list_connectors)

    github_item = EvidenceItem(
        source="commit", reference="r1", summary="s1", retrieved_at=datetime.now(timezone.utc)
    )
    slack_item = EvidenceItem(
        source="slack", reference="r2", summary="s2", retrieved_at=datetime.now(timezone.utc)
    )
    fake_github = _FakeLiveSource([github_item])
    fake_slack = _FakeLiveSource([slack_item])
    monkeypatch.setattr(
        evidence_module, "_LIVE_SOURCES", {"github": fake_github, "slack": fake_slack}
    )

    actor = Identity.for_agent("test_agent", uuid.uuid4())
    result = await _gather_live_evidence(
        session=None, query="checkout failing", actor=actor, retry_count={}
    )

    assert result == [github_item, slack_item]
    assert fake_github.calls == 1
    assert fake_slack.calls == 1


@pytest.mark.asyncio
async def test_gather_live_evidence_skips_inactive_connectors(monkeypatch) -> None:
    inactive_config = _connector_config("github", status="error")

    async def fake_list_connectors(session, actor, organization_id):
        return [inactive_config]

    monkeypatch.setattr(evidence_module.tenancy_service, "list_connectors", fake_list_connectors)
    fake_github = _FakeLiveSource(
        [EvidenceItem(source="commit", reference="r", summary="s", retrieved_at=datetime.now(timezone.utc))]
    )
    monkeypatch.setattr(evidence_module, "_LIVE_SOURCES", {"github": fake_github})

    actor = Identity.for_agent("test_agent", uuid.uuid4())
    result = await _gather_live_evidence(session=None, query="q", actor=actor, retry_count={})

    assert result == []
    assert fake_github.calls == 0


@pytest.mark.asyncio
async def test_gather_live_evidence_returns_empty_when_list_connectors_fails(monkeypatch) -> None:
    async def failing_list_connectors(session, actor, organization_id):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(evidence_module.tenancy_service, "list_connectors", failing_list_connectors)

    actor = Identity.for_agent("test_agent", uuid.uuid4())
    result = await _gather_live_evidence(session=None, query="q", actor=actor, retry_count={})

    assert result == []


@pytest.mark.asyncio
async def test_gather_live_evidence_logs_and_skips_source_that_raises(monkeypatch) -> None:
    """One connector's live lookup failing (bad token, rate limit, timeout)
    must not affect any other connector's -- same non-fatal discipline as
    every other source in this module.
    """
    import app.agents.retry as retry_module

    async def instant_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(retry_module.asyncio, "sleep", instant_sleep)

    github_config = _connector_config("github")

    async def fake_list_connectors(session, actor, organization_id):
        return [github_config]

    monkeypatch.setattr(evidence_module.tenancy_service, "list_connectors", fake_list_connectors)

    class _FailingLiveSource:
        async def fetch_live_evidence(self, **kwargs):
            raise RuntimeError("github search failed")

    monkeypatch.setattr(evidence_module, "_LIVE_SOURCES", {"github": _FailingLiveSource()})

    actor = Identity.for_agent("test_agent", uuid.uuid4())
    result = await _gather_live_evidence(session=None, query="q", actor=actor, retry_count={})

    assert result == []
