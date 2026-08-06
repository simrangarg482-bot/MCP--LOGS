"""Tests for `app.agents.knowledge_gap.pipeline.detect_knowledge_gaps` --
`repository`/`embedding`/`retrieval_service`/the LLM are all monkeypatched
with fakes (same style as `tests/agents/investigation/test_evidence.py`),
no real database, network, or model call.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.agents.knowledge_gap import pipeline as pipeline_module


class _FakeExecution:
    def __init__(self, query: str) -> None:
        self.id = uuid.uuid4()
        self.input_summary = {"query": query}


class _FakeExistingReport:
    def __init__(self, topic_embedding: list[float], supporting_execution_ids: list[str]) -> None:
        self.id = uuid.uuid4()
        self.topic_embedding = topic_embedding
        self.supporting_execution_ids = supporting_execution_ids


class _FakeLLMResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLM:
    def __init__(self, topic: str = "How to configure checkout retries") -> None:
        self.topic = topic
        self.calls = 0

    async def ainvoke(self, prompt: str) -> _FakeLLMResponse:
        self.calls += 1
        return _FakeLLMResponse(self.topic)


class _FakeScoredChunk:
    def __init__(self, document_id: uuid.UUID, score: float) -> None:
        self.document_id = document_id
        self.score = score


def _patch_common(
    monkeypatch,
    *,
    executions: list[_FakeExecution],
    existing_reports: list[_FakeExistingReport] | None = None,
    search_results: list[_FakeScoredChunk] | None = None,
):
    async def fake_list_low_confidence_executions(session, organization_id, **kwargs):
        return executions

    async def fake_list_open_gap_reports(session, organization_id):
        return existing_reports or []

    inserted: list[dict] = []

    async def fake_insert_gap_report(session, **kwargs):
        row = _FakeExistingReport(kwargs["topic_embedding"], kwargs["supporting_execution_ids"])
        row.suggested_topic = kwargs["suggested_topic"]
        row.suggested_action = kwargs["suggested_action"]
        row.related_document_id = kwargs["related_document_id"]
        inserted.append(kwargs)
        return row

    updated: list[dict] = []

    async def fake_update_gap_report_supporting_ids(session, gap_report_id, *, supporting_execution_ids):
        updated.append({"id": gap_report_id, "supporting_execution_ids": supporting_execution_ids})
        for report in existing_reports or []:
            if report.id == gap_report_id:
                report.supporting_execution_ids = supporting_execution_ids
                return report
        return None

    async def fake_embed_texts(texts):
        # Deterministic "embedding": one-hot-ish based on text identity, but
        # texts containing "checkout" cluster together, "auth" cluster
        # together -- close enough for a leader-clustering threshold test
        # without needing a real model.
        vectors = []
        for text in texts:
            if "checkout" in text:
                vectors.append([1.0, 0.0, 0.0])
            elif "auth" in text:
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return vectors

    async def fake_search(session, query, filters, top_k, collection=None, **kwargs):
        return search_results or []

    monkeypatch.setattr(
        pipeline_module.repository,
        "list_low_confidence_executions",
        fake_list_low_confidence_executions,
    )
    monkeypatch.setattr(
        pipeline_module.repository, "list_open_gap_reports", fake_list_open_gap_reports
    )
    monkeypatch.setattr(pipeline_module.repository, "insert_gap_report", fake_insert_gap_report)
    monkeypatch.setattr(
        pipeline_module.repository,
        "update_gap_report_supporting_ids",
        fake_update_gap_report_supporting_ids,
    )
    monkeypatch.setattr(pipeline_module.embedding, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(pipeline_module.retrieval_service, "search", fake_search)

    return inserted, updated


@pytest.mark.asyncio
async def test_returns_empty_when_fewer_queries_than_min_cluster_size(monkeypatch) -> None:
    executions = [_FakeExecution("why is checkout failing")]
    _patch_common(monkeypatch, executions=executions)

    result = await pipeline_module.detect_knowledge_gaps(
        None,
        _FakeLLM(),
        uuid.uuid4(),
        confidence_threshold=0.6,
        lookback=timedelta(days=14),
        min_cluster_size=3,
        similarity_threshold=0.82,
    )

    assert result == []


@pytest.mark.asyncio
async def test_creates_new_report_for_cluster_above_threshold(monkeypatch) -> None:
    executions = [
        _FakeExecution("why is checkout failing"),
        _FakeExecution("checkout keeps timing out"),
        _FakeExecution("checkout service errors"),
    ]
    inserted, _ = _patch_common(monkeypatch, executions=executions, search_results=[])
    llm = _FakeLLM(topic="Checkout service reliability")

    result = await pipeline_module.detect_knowledge_gaps(
        None,
        llm,
        uuid.uuid4(),
        confidence_threshold=0.6,
        lookback=timedelta(days=14),
        min_cluster_size=3,
        similarity_threshold=0.82,
    )

    assert len(result) == 1
    assert inserted[0]["suggested_topic"] == "Checkout service reliability"
    assert inserted[0]["suggested_action"] == "new_runbook"
    assert inserted[0]["related_document_id"] is None
    assert len(inserted[0]["supporting_execution_ids"]) == 3
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_skips_clusters_below_min_cluster_size(monkeypatch) -> None:
    executions = [
        _FakeExecution("why is checkout failing"),
        _FakeExecution("checkout keeps timing out"),
        _FakeExecution("auth token expired"),  # its own, too-small cluster
    ]
    inserted, _ = _patch_common(monkeypatch, executions=executions)

    result = await pipeline_module.detect_knowledge_gaps(
        None,
        _FakeLLM(),
        uuid.uuid4(),
        confidence_threshold=0.6,
        lookback=timedelta(days=14),
        min_cluster_size=3,
        similarity_threshold=0.82,
    )

    # Only the 2-member checkout cluster and 1-member auth cluster exist;
    # neither reaches min_cluster_size=3, so nothing is created.
    assert result == []
    assert inserted == []


@pytest.mark.asyncio
async def test_ignores_executions_without_query_in_input_summary(monkeypatch) -> None:
    incident_triage_execution = _FakeExecution("placeholder")
    incident_triage_execution.input_summary = {"incident_id": str(uuid.uuid4())}
    executions = [
        _FakeExecution("checkout failing"),
        _FakeExecution("checkout timeout"),
        incident_triage_execution,
    ]
    _patch_common(monkeypatch, executions=executions)

    result = await pipeline_module.detect_knowledge_gaps(
        None,
        _FakeLLM(),
        uuid.uuid4(),
        confidence_threshold=0.6,
        lookback=timedelta(days=14),
        min_cluster_size=3,
        similarity_threshold=0.82,
    )

    # Only 2 usable queries remain (below min_cluster_size=3) once the
    # query-less execution is filtered out.
    assert result == []


@pytest.mark.asyncio
async def test_merges_into_existing_open_report_instead_of_duplicating(monkeypatch) -> None:
    existing = _FakeExistingReport(topic_embedding=[1.0, 0.0, 0.0], supporting_execution_ids=["old-id"])
    executions = [
        _FakeExecution("checkout failing"),
        _FakeExecution("checkout timeout"),
        _FakeExecution("checkout errors"),
    ]
    inserted, updated = _patch_common(
        monkeypatch, executions=executions, existing_reports=[existing]
    )
    llm = _FakeLLM()

    result = await pipeline_module.detect_knowledge_gaps(
        None,
        llm,
        uuid.uuid4(),
        confidence_threshold=0.6,
        lookback=timedelta(days=14),
        min_cluster_size=3,
        similarity_threshold=0.82,
    )

    assert inserted == []  # merged, not duplicated
    assert len(updated) == 1
    assert "old-id" in updated[0]["supporting_execution_ids"]
    assert len(result) == 1
    # No topic synthesis needed for a merge -- the LLM is never called.
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_resolves_update_existing_when_document_match_found(monkeypatch) -> None:
    document_id = uuid.uuid4()
    executions = [
        _FakeExecution("checkout failing"),
        _FakeExecution("checkout timeout"),
        _FakeExecution("checkout errors"),
    ]
    inserted, _ = _patch_common(
        monkeypatch,
        executions=executions,
        search_results=[_FakeScoredChunk(document_id, 0.75)],
    )

    await pipeline_module.detect_knowledge_gaps(
        None,
        _FakeLLM(),
        uuid.uuid4(),
        confidence_threshold=0.6,
        lookback=timedelta(days=14),
        min_cluster_size=3,
        similarity_threshold=0.82,
    )

    assert inserted[0]["suggested_action"] == "update_existing"
    assert inserted[0]["related_document_id"] == document_id
