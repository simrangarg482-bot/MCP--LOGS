"""Tests for derived-data cleanup: a deleted source document must neither
leave its chunks/embeddings in storage nor remain retrievable.

This covers the concrete data-lifecycle bug found during Priority 3's
discovery pass, which is worth restating because it is the reason these
tests exist:

    `core.knowledge.service.reject_document` soft-deleted a document by
    setting `documents.deleted_at`. `core/knowledge`'s own reads filter on
    that column, so the document correctly vanished from the review UI.
    But the derived rows in `documentation_chunks`/`code_chunks`/
    `conversations_chunks` -- each carrying its own copy of the text plus
    its embedding -- were never purged, and `retrieval.pgvector.store`'s
    queries did not filter on `deleted_at` either (they joined `documents`
    only for title/source_url). Net effect: a human could reject a
    document and the Answer Agent would still retrieve, quote and cite it.

Two independent barriers now exist, and both are tested here, because
either one alone leaves a real hole: the query-time filter still leaves
rejected content sitting in storage, and the purge alone would not protect
rows soft-deleted before this change shipped.

No database is available in this suite, so the query-time filter is
verified by compiling the statement and asserting the predicate is present
(same approach and same disclosed limitation as
`test_repository_scoping.py`), and the purge is verified through the
service's real call sequence.
"""

from __future__ import annotations

import uuid
from datetime import UTC

import pytest

from app.retrieval.pgvector.store import PgVectorStore
from app.retrieval.schemas import SearchFilters


def _compile(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def _compile_structure(stmt) -> str:
    """Render a statement WITHOUT literal binds, for queries whose bind
    values cannot be rendered literally.

    `lexical_search` passes `'english'` to `to_tsquery` as a `REGCONFIG`,
    which SQLAlchemy has no literal renderer for. That does not matter for
    what these tests check: `deleted_at IS NULL` is a structural predicate
    with no bind parameter, so it appears identically either way.
    """
    return str(stmt.compile())


class _CapturingSession:
    """Captures the SELECT the store builds, returning no rows."""

    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        return _EmptyResult()

    async def flush(self):  # pragma: no cover - not exercised here
        return None


class _EmptyResult:
    def all(self):
        return []

    def scalar_one(self):
        return 0

    rowcount = 0


@pytest.fixture()
def filters() -> SearchFilters:
    return SearchFilters(organization_id=uuid.uuid4(), permission_codes=frozenset())


# --- query-time exclusion of soft-deleted documents -----------------------


@pytest.mark.parametrize("collection", ["documentation", "code", "conversations"])
@pytest.mark.asyncio
async def test_dense_search_excludes_soft_deleted_documents(collection, filters):
    session = _CapturingSession()
    store = PgVectorStore()

    await store.search(session, collection, [0.1] * 384, filters, top_k=5)

    sql = _compile(session.statements[0]).lower().replace(" ", "")
    assert "documents.deleted_atisnull" in sql, (
        f"dense search on {collection!r} does not exclude soft-deleted documents -- "
        "rejected content would remain retrievable"
    )


@pytest.mark.parametrize("collection", ["documentation", "code", "conversations"])
@pytest.mark.asyncio
async def test_lexical_search_excludes_soft_deleted_documents(collection, filters):
    """The lexical half needs the predicate independently: fusing a filtered
    dense list with an unfiltered lexical one reintroduces the whole leak."""
    session = _CapturingSession()
    store = PgVectorStore()

    await store.lexical_search(session, collection, "connection pool", filters, top_k=5)

    sql = _compile_structure(session.statements[0]).lower().replace(" ", "")
    assert "documents.deleted_atisnull" in sql, (
        f"lexical search on {collection!r} does not exclude soft-deleted documents"
    )


@pytest.mark.asyncio
async def test_soft_delete_filter_coexists_with_tenant_and_acl_filters(filters):
    """The new predicate must not have displaced the existing isolation
    filters -- all three have to hold at once."""
    session = _CapturingSession()
    store = PgVectorStore()

    await store.search(session, "documentation", [0.1] * 384, filters, top_k=5)

    sql = _compile(session.statements[0]).lower().replace(" ", "")
    assert "documents.deleted_atisnull" in sql
    assert "organization_id" in sql
    assert "acl_permission_code" in sql


# --- storage purge on rejection -------------------------------------------


@pytest.mark.asyncio
async def test_rejecting_a_document_purges_chunks_from_every_collection(monkeypatch):
    """Storage cleanup, not just hiding: the chunk rows (and their
    embeddings) must actually be deleted, across all three collections,
    since a `documents` row can be populated by ingestion into any of them.
    """
    from datetime import datetime

    from app.core.knowledge import service as knowledge_service
    from app.shared.schemas import ActorKind, Identity

    organization_id = uuid.uuid4()
    document_id = uuid.uuid4()
    project_id = uuid.uuid4()

    class _Row:
        def __init__(self) -> None:
            self.id = document_id
            self.organization_id = organization_id
            self.project_id = project_id
            self.title = "A runbook"
            self.status = "proposed"
            self.version = 1
            self.source = "manual"
            self.source_url = None
            self.deleted_at = None
            self.created_at = datetime.now(UTC)
            self.updated_at = datetime.now(UTC)

    row = _Row()
    actor = Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        permissions=frozenset({"knowledge:review"}),
    )

    purged: list[tuple[str, uuid.UUID]] = []

    async def fake_get_document_by_id(session, doc_id):
        return row

    async def fake_get_metadata_value(session, doc_id, key):
        return None

    async def fake_soft_delete_document(session, doc_id, *, deleted_at):
        row.deleted_at = deleted_at
        return row

    async def fake_record_audit_event(session, actor_, **kwargs):
        return None

    async def fake_delete(session, collection, doc_id):
        purged.append((collection, doc_id))

    async def fake_remove_edges_for_entity(session, *, organization_id, entity_type, entity_id):
        return 0

    async def fake_handle_evidence_entity_removed(
        session, *, organization_id, entity_type, entity_id
    ):
        return 0

    monkeypatch.setattr(
        knowledge_service.repository, "get_document_by_id", fake_get_document_by_id
    )
    monkeypatch.setattr(
        knowledge_service.repository, "get_metadata_value", fake_get_metadata_value
    )
    monkeypatch.setattr(
        knowledge_service.repository, "soft_delete_document", fake_soft_delete_document
    )
    monkeypatch.setattr(knowledge_service, "record_audit_event", fake_record_audit_event)
    monkeypatch.setattr(knowledge_service.retrieval_service, "delete", fake_delete)
    # `reject_document` also asks `core.graph.service` to purge any stored
    # graph edge naming this document (deferred-imported to avoid a circular
    # import -- see that call site's comment); patched here for the same
    # reason every other repository call in this test is faked.
    from app.core.graph import service as graph_service
    from app.core.proactive import service as proactive_service

    monkeypatch.setattr(graph_service, "remove_edges_for_entity", fake_remove_edges_for_entity)
    # Same reasoning, one hook further: `core.proactive.service`'s evidence
    # cleanup, also deferred-imported by `reject_document`.
    monkeypatch.setattr(
        proactive_service, "handle_evidence_entity_removed", fake_handle_evidence_entity_removed
    )

    await knowledge_service.reject_document(None, actor, organization_id, document_id)

    assert purged == [
        ("documentation", document_id),
        ("code", document_id),
        ("conversations", document_id),
    ]


@pytest.mark.asyncio
async def test_chunk_purge_is_idempotent_across_collections(monkeypatch):
    """Purging a collection with no matching chunks is a successful no-op --
    which is what makes sweeping all three collections safe rather than
    error-prone."""
    from app.core.knowledge import service as knowledge_service

    session = _CapturingSession()
    document_id = uuid.uuid4()

    store = PgVectorStore()
    # Deleting twice must not raise; the second call simply matches nothing.
    await store.delete(session, "documentation", document_id)
    await store.delete(session, "documentation", document_id)

    assert len(session.statements) == 2
    for statement in session.statements:
        sql = _compile(statement).lower()
        assert sql.startswith("delete from documentation_chunks")

    # And the service-level sweep reports every collection it covered.
    purged = await knowledge_service._purge_document_chunks(session, document_id)
    assert purged == ["documentation", "code", "conversations"]
