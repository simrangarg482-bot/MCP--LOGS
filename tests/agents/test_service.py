"""Tests for `app.agents.service`'s Milestone 8 additions:
`search_similar_incidents`, `search_recent_changes`, and the latter's
`_passes_recency_filter` best-effort `since` check -- plus Milestone 9's
`detect_knowledge_gaps`/`list_gap_reports`.

Follows the same `monkeypatch.setattr(<module>.<dependency>, ...)` style
already established in `tests/agents/investigation/test_evidence.py` --
patching the shared `app.retrieval.service` module object via the alias
`app.agents.service` imports it under (`retrieval_service`), not a copy of
it, so the patch is visible to `agents_service.search_similar_incidents`/
`search_recent_changes` exactly as it would be in production.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.agents import service as agents_service
from app.agents.service import _passes_recency_filter
from app.core.exceptions import PermissionDeniedError
from app.retrieval.schemas import ScoredChunk, SearchFilters
from app.shared.schemas import ActorKind, Identity


def _reviewer(organization_id: uuid.UUID) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        permissions=frozenset({"knowledge:review"}),
    )


def _chunk(metadata: dict[str, str] | None = None, content: str = "content") -> ScoredChunk:
    return ScoredChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        collection="code",
        content=content,
        score=0.8,
        source_offset_start=0,
        source_offset_end=len(content),
        title="a title",
        source_url="https://github.com/acme/widgets/commit/abc123",
        metadata=metadata or {},
    )


@pytest.mark.asyncio
async def test_search_similar_incidents_scopes_filters_to_actor_org(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_search(session, query, filters, top_k, *args, **kwargs):
        captured["session"] = session
        captured["query"] = query
        captured["filters"] = filters
        captured["top_k"] = top_k
        captured["args"] = args
        return [_chunk()]

    monkeypatch.setattr(agents_service.retrieval_service, "search", fake_search)

    actor = Identity.for_agent("test_agent", uuid.uuid4())
    result = await agents_service.search_similar_incidents(None, "checkout failing", actor)

    assert len(result) == 1
    assert isinstance(captured["filters"], SearchFilters)
    assert captured["filters"].organization_id == actor.organization_id
    assert captured["top_k"] == 10
    # No collection is passed -- `retrieval.search`'s own all-collections
    # default (`collection=None`) applies, since no "incidents" collection
    # exists (see the function's own docstring for why).
    assert captured["args"] == ()


@pytest.mark.asyncio
async def test_search_recent_changes_default_searches_both_documentation_and_code(
    monkeypatch,
) -> None:
    """Regression test for a real bug: this used to search exactly one
    collection by default (first `"code"`, then, in an earlier fix,
    `"documentation"` alone). GitHub commits, PR bodies, and issue bodies
    have no file extension, so `app.ingestion.processors.chunking.
    classify_content_type` never classifies them as `"code"` -- they land
    in `"documentation"`, same as READMEs and other docs (see
    `test_search_recent_changes_default_collections_include_where_commit_
    and_issue_content_is_classified` below for the direct proof tying this
    to that classification) -- but a repo's actual changed source files
    still land in `"code"`. Searching only one of the two silently hid the
    other's evidence, so the default must search both and fuse the results,
    not restrict to either alone.
    """
    calls: list[dict[str, object]] = []
    # Same chunk object/chunk_id returned for both collections, so a
    # correct fusion collapses it back to one result (proving genuine RRF
    # dedup by identity, not just "both calls happened").
    shared_chunk = _chunk()

    async def fake_search(session, query, filters, top_k, collection=None, *, include_metadata=False):
        calls.append({"collection": collection, "include_metadata": include_metadata})
        return [shared_chunk]

    monkeypatch.setattr(agents_service.retrieval_service, "search", fake_search)

    actor = Identity.for_agent("test_agent", uuid.uuid4())
    result = await agents_service.search_recent_changes(None, "checkout", actor)

    assert len(result) == 1
    assert {call["collection"] for call in calls} == {"documentation", "code"}
    assert all(call["include_metadata"] is True for call in calls)


@pytest.mark.asyncio
async def test_search_recent_changes_default_collections_include_where_commit_and_issue_content_is_classified(
    monkeypatch,
) -> None:
    """The direct proof this fix is correct, not just consistent with
    itself: independently classify synthetic GitHub commit/PR/issue-shaped
    content the same way ingestion actually does
    (`classify_content_type` -> `_CONTENT_TYPE_TO_COLLECTION`, both
    untouched by this fix), then assert `search_recent_changes`'s own
    default collections *include* the one that mapping actually produces
    for that content -- rather than merely asserting a literal string,
    which would pass even if both the default and the classification/
    mapping logic drifted out of sync in the same wrong direction.
    """
    from app.ingestion.processors.chunking import classify_content_type
    from app.ingestion.schemas import RawDocument
    from app.ingestion.service import _CONTENT_TYPE_TO_COLLECTION

    # Shaped like real GitHub connector output (PROJECT_PLAN.md section
    # 4.1's `normalize()` output): a commit message, a PR body, and an
    # issue body -- none has a `path`/`title`/`external_id` ending in a
    # code file extension, per `classify_content_type`'s own logic.
    commit = RawDocument(
        source="github",
        external_id="abc123",
        content="Fix checkout race condition under concurrent requests",
        title="Fix checkout race condition",
    )
    pull_request = RawDocument(
        source="github",
        external_id="pr-42",
        content="This PR fixes the checkout bug described in #41.",
        title="Fix checkout bug",
    )
    issue = RawDocument(
        source="github",
        external_id="issue-41",
        content="Checkout fails intermittently under load.",
        title="Checkout fails intermittently",
    )

    for raw_document in (commit, pull_request, issue):
        content_type = classify_content_type(raw_document)
        assert content_type == "document", (
            f"{raw_document.external_id} classified as {content_type!r}, not "
            "'document' -- this test's premise (commit/PR/issue content has "
            "no code file extension) no longer holds, so the collections "
            "asserted below need re-deriving, not just re-asserting"
        )
        expected_collection = _CONTENT_TYPE_TO_COLLECTION[content_type]

        calls: list[dict[str, object]] = []

        async def fake_search(
            session, query, filters, top_k, collection=None, *, include_metadata=False
        ):
            calls.append({"collection": collection})
            return []

        monkeypatch.setattr(agents_service.retrieval_service, "search", fake_search)

        actor = Identity.for_agent("test_agent", uuid.uuid4())
        await agents_service.search_recent_changes(None, "checkout", actor)

        searched_collections = {call["collection"] for call in calls}
        assert expected_collection in searched_collections
        # And "code" (the repo's actual changed source files) is searched
        # alongside it -- this is the fix the previous single-collection
        # default missed.
        assert "code" in searched_collections


@pytest.mark.asyncio
async def test_search_recent_changes_default_fuses_results_from_both_collections(
    monkeypatch,
) -> None:
    """The two collections are genuinely fused, not just both queried and
    one discarded: a chunk found only in `"code"` and a different chunk
    found only in `"documentation"` must both survive into the final
    result.
    """
    documentation_chunk = _chunk(content="commit message content")
    code_chunk = _chunk(content="def checkout(): ...")

    async def fake_search(session, query, filters, top_k, collection=None, *, include_metadata=False):
        if collection == "documentation":
            return [documentation_chunk]
        if collection == "code":
            return [code_chunk]
        raise AssertionError(f"unexpected collection {collection!r}")

    monkeypatch.setattr(agents_service.retrieval_service, "search", fake_search)

    actor = Identity.for_agent("test_agent", uuid.uuid4())
    result = await agents_service.search_recent_changes(None, "checkout", actor)

    assert {chunk.chunk_id for chunk in result} == {documentation_chunk.chunk_id, code_chunk.chunk_id}


@pytest.mark.asyncio
async def test_search_recent_changes_explicit_collection_still_restricts_to_one(
    monkeypatch,
) -> None:
    """A caller that explicitly wants one collection only (e.g. the
    Investigation Agent's collection-scoped evidence-gathering steps,
    PROJECT_PLAN.md section 6.4) still gets exactly that -- the new
    both-collections behavior is only the *default* (`collection=None`),
    not something forced on every caller.
    """
    calls: list[dict[str, object]] = []

    async def fake_search(session, query, filters, top_k, collection=None, *, include_metadata=False):
        calls.append({"collection": collection})
        return [_chunk()]

    monkeypatch.setattr(agents_service.retrieval_service, "search", fake_search)

    actor = Identity.for_agent("test_agent", uuid.uuid4())
    result = await agents_service.search_recent_changes(None, "checkout", actor, collection="code")

    assert len(result) == 1
    assert len(calls) == 1
    assert calls[0]["collection"] == "code"


@pytest.mark.asyncio
async def test_search_recent_changes_filters_out_stale_chunks(monkeypatch) -> None:
    since = datetime(2026, 7, 15, tzinfo=timezone.utc)
    fresh = _chunk({"source_timestamp": "2026-07-20T00:00:00Z"})
    stale = _chunk({"source_timestamp": "2026-07-01T00:00:00Z"})
    no_timestamp = _chunk({})

    async def fake_search(*args, **kwargs):
        return [fresh, stale, no_timestamp]

    monkeypatch.setattr(agents_service.retrieval_service, "search", fake_search)

    actor = Identity.for_agent("test_agent", uuid.uuid4())
    # Pinned to one explicit collection -- this test is about the
    # recency filter, not collection selection (covered above), and
    # pinning avoids this fake's identical chunk_ids being queried twice
    # (once per default collection) and fused, which would just be
    # exercising RRF's own dedup logic instead of `_passes_recency_filter`.
    result = await agents_service.search_recent_changes(
        None, "checkout", actor, since=since, collection="documentation"
    )

    # `stale` is dropped; `no_timestamp` is kept (see docstring: "no
    # timestamp available" is not the same claim as "not recent").
    assert result == [fresh, no_timestamp]


@pytest.mark.asyncio
async def test_search_recent_changes_returns_everything_without_since(monkeypatch) -> None:
    chunks = [_chunk({"source_timestamp": "2020-01-01T00:00:00Z"}), _chunk({})]

    async def fake_search(*args, **kwargs):
        return chunks

    monkeypatch.setattr(agents_service.retrieval_service, "search", fake_search)

    actor = Identity.for_agent("test_agent", uuid.uuid4())
    # Same pinning as the test above, for the same reason.
    result = await agents_service.search_recent_changes(None, "checkout", actor, collection="documentation")

    assert result == chunks


def test_passes_recency_filter_keeps_chunk_with_no_recognized_metadata_key() -> None:
    chunk = _chunk({"unrelated_key": "value"})
    assert _passes_recency_filter(chunk, datetime.now(timezone.utc)) is True


def test_passes_recency_filter_handles_zulu_suffix() -> None:
    chunk = _chunk({"timestamp": "2026-08-01T00:00:00Z"})
    since = datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert _passes_recency_filter(chunk, since) is True


def test_passes_recency_filter_treats_naive_timestamp_as_utc() -> None:
    chunk = _chunk({"updated_at": "2026-08-01T00:00:00"})
    since = datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert _passes_recency_filter(chunk, since) is True

    since_future = datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert _passes_recency_filter(chunk, since_future) is False


def test_passes_recency_filter_ignores_unparseable_value_and_checks_next_key() -> None:
    chunk = _chunk({"source_timestamp": "not-a-date", "updated_at": "2026-08-01T00:00:00Z"})
    since = datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert _passes_recency_filter(chunk, since) is True


class _FakeExecutionRow:
    def __init__(self, execution_id: uuid.UUID) -> None:
        self.id = execution_id


@pytest.mark.asyncio
async def test_detect_knowledge_gaps_records_agent_execution_and_returns_reports(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    actor = Identity.for_agent("knowledge_gap_agent", organization_id)
    execution_id = uuid.uuid4()
    recorded: dict[str, object] = {}

    async def fake_insert_agent_execution(session, **kwargs):
        recorded["insert"] = kwargs
        return _FakeExecutionRow(execution_id)

    async def fake_update_agent_execution(session, exec_id, **kwargs):
        recorded["update"] = {"id": exec_id, **kwargs}

    from app.database.models.agent_models import KnowledgeGapReport

    fake_row = KnowledgeGapReport(
        id=uuid.uuid4(),
        organization_id=organization_id,
        suggested_topic="Checkout reliability",
        topic_embedding=[1.0, 0.0],
        supporting_execution_ids=[str(uuid.uuid4())],
        suggested_action="new_runbook",
        related_document_id=None,
        status="open",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    async def fake_pipeline(session, llm, org_id, **kwargs):
        assert org_id == organization_id
        return [fake_row]

    monkeypatch.setattr(agents_service.repository, "insert_agent_execution", fake_insert_agent_execution)
    monkeypatch.setattr(agents_service.repository, "update_agent_execution", fake_update_agent_execution)
    monkeypatch.setattr(agents_service, "_run_knowledge_gap_pipeline", fake_pipeline)

    reports = await agents_service.detect_knowledge_gaps(None, actor)

    assert len(reports) == 1
    assert reports[0].suggested_topic == "Checkout reliability"
    assert recorded["insert"]["agent_name"] == "detect_knowledge_gaps"
    assert recorded["insert"]["trigger_source"] == "scheduled"
    assert recorded["update"]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_detect_knowledge_gaps_marks_failed_and_reraises_on_error(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    actor = Identity.for_agent("knowledge_gap_agent", organization_id)
    execution_id = uuid.uuid4()
    recorded: dict[str, object] = {}

    async def fake_insert_agent_execution(session, **kwargs):
        return _FakeExecutionRow(execution_id)

    async def fake_update_agent_execution(session, exec_id, **kwargs):
        recorded["update"] = kwargs

    async def failing_pipeline(session, llm, org_id, **kwargs):
        raise RuntimeError("clustering blew up")

    monkeypatch.setattr(agents_service.repository, "insert_agent_execution", fake_insert_agent_execution)
    monkeypatch.setattr(agents_service.repository, "update_agent_execution", fake_update_agent_execution)
    monkeypatch.setattr(agents_service, "_run_knowledge_gap_pipeline", failing_pipeline)

    with pytest.raises(RuntimeError):
        await agents_service.detect_knowledge_gaps(None, actor)

    assert recorded["update"]["status"] == "failed"


@pytest.mark.asyncio
async def test_list_gap_reports_requires_knowledge_review_permission() -> None:
    actor = Identity.for_agent("some_agent", uuid.uuid4())
    with pytest.raises(PermissionDeniedError):
        await agents_service.list_gap_reports(None, actor)


@pytest.mark.asyncio
async def test_list_gap_reports_returns_reports_for_reviewer(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    actor = _reviewer(organization_id)

    from app.database.models.agent_models import KnowledgeGapReport

    fake_row = KnowledgeGapReport(
        id=uuid.uuid4(),
        organization_id=organization_id,
        suggested_topic="Auth token expiry",
        topic_embedding=[0.0, 1.0],
        supporting_execution_ids=[str(uuid.uuid4()), str(uuid.uuid4())],
        suggested_action="update_existing",
        related_document_id=uuid.uuid4(),
        status="open",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    async def fake_list_open_gap_reports(session, org_id):
        assert org_id == organization_id
        return [fake_row]

    monkeypatch.setattr(
        agents_service.knowledge_gap_repository, "list_open_gap_reports", fake_list_open_gap_reports
    )

    reports = await agents_service.list_gap_reports(None, actor)

    assert len(reports) == 1
    assert reports[0].suggested_action == "update_existing"
    assert len(reports[0].supporting_execution_ids) == 2


def _observability_reader(organization_id: uuid.UUID) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        permissions=frozenset({"observability:read"}),
    )


@pytest.mark.asyncio
async def test_get_agent_execution_stats_requires_permission() -> None:
    actor = Identity.for_agent("some_agent", uuid.uuid4())
    with pytest.raises(PermissionDeniedError):
        await agents_service.get_agent_execution_stats(None, actor)


@pytest.mark.asyncio
async def test_get_agent_execution_stats_maps_aggregate_rows(monkeypatch) -> None:
    from types import SimpleNamespace

    organization_id = uuid.uuid4()
    actor = _observability_reader(organization_id)
    rows = [
        SimpleNamespace(
            agent_name="answer_question",
            execution_count=20,
            succeeded_count=18,
            failed_count=2,
            avg_confidence_score=0.72,
            avg_latency_seconds=1.5,
            total_prompt_tokens=12000,
            total_completion_tokens=3000,
            total_tokens=15000,
        ),
        SimpleNamespace(
            agent_name="detect_knowledge_gaps",
            execution_count=5,
            succeeded_count=5,
            failed_count=0,
            avg_confidence_score=None,
            avg_latency_seconds=None,
            total_prompt_tokens=None,
            total_completion_tokens=None,
            total_tokens=None,
        ),
    ]

    async def fake_get_agent_execution_stats(session, org_id, *, since=None):
        assert org_id == organization_id
        return rows

    monkeypatch.setattr(
        agents_service.repository, "get_agent_execution_stats", fake_get_agent_execution_stats
    )

    result = await agents_service.get_agent_execution_stats(None, actor)

    assert len(result) == 2
    assert result[0].agent_name == "answer_question"
    assert result[0].succeeded_count == 18
    assert result[0].failed_count == 2
    assert result[0].avg_confidence_score == 0.72
    assert result[0].total_prompt_tokens == 12000
    assert result[0].total_completion_tokens == 3000
    assert result[0].total_tokens == 15000
    # gpt-4o-mini pricing: (12000/1e6)*0.15 + (3000/1e6)*0.60 = 0.0018 + 0.0018
    assert result[0].estimated_cost_usd == 0.0036
    assert result[1].avg_confidence_score is None
    assert result[1].avg_latency_seconds is None
    assert result[1].total_tokens is None
    assert result[1].estimated_cost_usd is None
