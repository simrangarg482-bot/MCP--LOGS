"""Tests for `app.core.memory.service` -- creation, recall selection,
lifecycle, and the deletion guarantee.

`repository.py` and `embedding` are monkeypatched with in-memory fakes (the
same style as `tests/core/knowledge/test_service.py` and
`tests/agents/knowledge_gap/test_pipeline.py`, which already patches
`embedding.embed_texts`). No database, no model download, no paid API --
which is the point: the whole memory subsystem must be testable and CI-safe
without external services.

The fake embedder is deliberately a crude bag-of-words vector rather than a
random one, so "relevant ranks above irrelevant" is a real, deterministic
assertion about the ranking path instead of a tautology.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from app.core.memory import service as memory_service
from app.core.memory.schemas import MemoryCreate, MemoryUpdate, RecalledMemory
from app.shared.schemas import ActorKind, Identity

_DIM = 384
# A small fixed vocabulary; each word owns one dimension. Cosine distance
# between two of these vectors is then a genuine function of shared words.
_VOCAB = ["deployment", "database", "pool", "billing", "invoice", "region", "europe"]


def _fake_embed(text: str) -> list[float]:
    vector = [0.0] * _DIM
    lowered = text.lower()
    for index, word in enumerate(_VOCAB):
        if word in lowered:
            vector[index] = 1.0
    if not any(vector):
        vector[_DIM - 1] = 1.0  # orthogonal to every vocabulary word
    norm = sum(v * v for v in vector) ** 0.5
    return [v / norm for v in vector]


def _cosine_distance(a: list[float], b: list[float]) -> float:
    return 1.0 - sum(x * y for x, y in zip(a, b, strict=True))


def _user(organization_id: uuid.UUID, *, projects: dict | None = None) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        project_permissions=projects or {},
    )


class _Row:
    """Stand-in for an `AgentMemory` ORM row."""

    def __init__(self, **kwargs):
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        self.id = kwargs.get("id", uuid.uuid4())
        self.organization_id = kwargs["organization_id"]
        self.scope = kwargs.get("scope", "user")
        self.owner_user_id = kwargs.get("owner_user_id")
        self.project_id = kwargs.get("project_id")
        self.memory_type = kwargs.get("memory_type", "fact")
        self.content = kwargs.get("content", "some memory")
        self.embedding = kwargs.get("embedding", _fake_embed(self.content))
        self.source_type = kwargs.get("source_type", "explicit")
        self.source_id = kwargs.get("source_id")
        self.created_by = kwargs.get("created_by", "user:someone")
        self.status = kwargs.get("status", "active")
        self.supersedes_memory_id = kwargs.get("supersedes_memory_id")
        self.memory_metadata = kwargs.get("memory_metadata")
        self.last_accessed_at = None
        self.created_at = now
        self.updated_at = now


@pytest.fixture()
def store(monkeypatch):
    """An in-memory memory store wired under the service's repository calls."""
    state: dict = {"rows": [], "calls": [], "touched": []}

    async def fake_embed_query(text: str):
        return _fake_embed(text)

    monkeypatch.setattr(memory_service.embedding, "embed_query", fake_embed_query)

    async def fake_insert(session, **kwargs):
        row = _Row(**kwargs)
        state["rows"].append(row)
        state["calls"].append("insert")
        return row

    async def fake_recall(
        session, *, organization_id, actor_user_id, allowed_project_ids, query_embedding, limit
    ):
        """Mirrors the real SQL's semantics: filter first, then rank."""
        visible = [
            r
            for r in state["rows"]
            if r.organization_id == organization_id
            and r.status == "active"
            and (
                (r.scope == "user" and actor_user_id is not None
                 and r.owner_user_id == actor_user_id)
                or (r.scope == "project" and r.project_id in set(allowed_project_ids))
            )
        ]
        scored = [(r, _cosine_distance(query_embedding, r.embedding)) for r in visible]
        scored.sort(key=lambda pair: pair[1])
        return scored[:limit]

    async def fake_get_visible(
        session, memory_id, *, organization_id, actor_user_id, allowed_project_ids
    ):
        for r in state["rows"]:
            if (
                r.id == memory_id
                and r.organization_id == organization_id
                and r.status == "active"
                and (
                    (r.scope == "user" and r.owner_user_id == actor_user_id)
                    or (r.scope == "project" and r.project_id in set(allowed_project_ids))
                )
            ):
                return r
        return None

    async def fake_get_any_status(session, memory_id, *, organization_id):
        for r in state["rows"]:
            if r.id == memory_id and r.organization_id == organization_id:
                return r
        return None

    async def fake_soft_delete(session, memory_id):
        for r in state["rows"]:
            if r.id == memory_id and r.status != "deleted":
                r.status = "deleted"
                r.content = ""
                r.embedding = [0.0] * _DIM
                state["calls"].append("soft_delete")
                return 1
        return 0

    async def fake_mark_superseded(session, memory_id):
        for r in state["rows"]:
            if r.id == memory_id and r.status == "active":
                r.status = "superseded"
                state["calls"].append("mark_superseded")
                return 1
        return 0

    async def fake_update_content(session, memory_id, *, content, embedding, memory_metadata):
        for r in state["rows"]:
            if r.id == memory_id:
                r.content = content
                r.embedding = embedding
                r.memory_metadata = memory_metadata
                state["calls"].append("update_content")
                return r
        return None

    async def fake_list_visible(
        session, *, organization_id, actor_user_id, allowed_project_ids, limit, offset
    ):
        visible = [
            r
            for r in state["rows"]
            if r.organization_id == organization_id
            and r.status == "active"
            and (
                (r.scope == "user" and r.owner_user_id == actor_user_id)
                or (r.scope == "project" and r.project_id in set(allowed_project_ids))
            )
        ]
        return visible[offset : offset + limit]

    async def fake_touch(session, memory_ids, *, accessed_at):
        state["touched"].extend(memory_ids)
        return len(memory_ids)

    repo = memory_service.repository
    monkeypatch.setattr(repo, "insert", fake_insert)
    monkeypatch.setattr(repo, "recall", fake_recall)
    monkeypatch.setattr(repo, "get_visible", fake_get_visible)
    monkeypatch.setattr(repo, "get_any_status_for_owner", fake_get_any_status)
    monkeypatch.setattr(repo, "soft_delete", fake_soft_delete)
    monkeypatch.setattr(repo, "mark_superseded", fake_mark_superseded)
    monkeypatch.setattr(repo, "update_content", fake_update_content)
    monkeypatch.setattr(repo, "list_visible", fake_list_visible)
    monkeypatch.setattr(repo, "touch_last_accessed", fake_touch)

    async def fake_audit(session, actor, **kwargs):
        state["calls"].append(f"audit:{kwargs.get('action')}")
        state.setdefault("audit", []).append(kwargs)

    monkeypatch.setattr(memory_service, "record_audit_event", fake_audit)
    return state


# --- creation --------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_user_scoped_memory_owns_it_to_the_actor(store):
    actor = _user(uuid.uuid4())
    memory = await memory_service.create_memory(
        None,
        actor,
        MemoryCreate(scope="user", memory_type="preference", content="Our region is europe"),
    )
    assert memory.scope == "user"
    assert memory.owner_user_id == actor.user_id
    assert memory.created_by == actor.audit_tag


@pytest.mark.asyncio
async def test_create_user_scoped_memory_rejects_an_agent_identity(store):
    """An agent has no `users` row, so it cannot own private memory."""
    agent = Identity.for_agent("some_agent", uuid.uuid4())
    with pytest.raises(ValidationError, match="user identity"):
        await memory_service.create_memory(
            None, agent, MemoryCreate(scope="user", memory_type="fact", content="x")
        )


@pytest.mark.asyncio
async def test_create_project_memory_requires_access_to_that_project(store):
    actor = _user(uuid.uuid4())  # no project memberships
    with pytest.raises(PermissionDeniedError, match="project"):
        await memory_service.create_memory(
            None,
            actor,
            MemoryCreate(
                scope="project",
                memory_type="fact",
                content="deployment notes",
                project_id=uuid.uuid4(),
            ),
        )


@pytest.mark.asyncio
async def test_create_project_memory_succeeds_with_membership(store):
    project_id = uuid.uuid4()
    actor = _user(uuid.uuid4(), projects={project_id: frozenset({"incident:write"})})
    memory = await memory_service.create_memory(
        None,
        actor,
        MemoryCreate(
            scope="project", memory_type="fact", content="deployment notes", project_id=project_id
        ),
    )
    assert memory.scope == "project"
    assert memory.project_id == project_id
    assert memory.owner_user_id is None


def test_scope_and_field_mismatches_are_rejected_by_the_schema():
    with pytest.raises(ValueError, match="requires project_id"):
        MemoryCreate(scope="project", memory_type="fact", content="x")
    with pytest.raises(ValueError, match="cannot carry a project_id"):
        MemoryCreate(scope="user", memory_type="fact", content="x", project_id=uuid.uuid4())


def test_empty_content_is_rejected():
    with pytest.raises(ValueError):
        MemoryCreate(scope="user", memory_type="fact", content="   ")


@pytest.mark.asyncio
async def test_create_audits_without_recording_the_content(store):
    """A user-private memory's text must not be copied into the org-readable
    audit trail (`audit:read` is an org-level permission)."""
    actor = _user(uuid.uuid4())
    secret = "my private deployment region is europe"
    await memory_service.create_memory(
        None, actor, MemoryCreate(scope="user", memory_type="preference", content=secret)
    )
    events = store["audit"]
    assert len(events) == 1
    assert secret not in str(events[0])
    assert events[0]["metadata"]["scope"] == "user"


# --- recall: relevance, isolation, budget ---------------------------------


@pytest.mark.asyncio
async def test_relevant_memory_ranks_above_irrelevant(store):
    actor = _user(uuid.uuid4())
    await memory_service.create_memory(
        None,
        actor,
        MemoryCreate(scope="user", memory_type="fact", content="database pool sizing"),
    )
    await memory_service.create_memory(
        None, actor, MemoryCreate(scope="user", memory_type="fact", content="billing invoice")
    )

    recalled = await memory_service.recall_relevant(None, actor, "database pool")
    assert recalled, "a clearly relevant memory should be recalled"
    assert "database pool" in recalled[0].content
    assert all("billing" not in m.content for m in recalled)


@pytest.mark.asyncio
async def test_unrelated_query_recalls_nothing(store):
    """The relevance threshold must exclude weak matches rather than
    returning the least-bad memory available."""
    actor = _user(uuid.uuid4())
    await memory_service.create_memory(
        None, actor, MemoryCreate(scope="user", memory_type="fact", content="billing invoice")
    )
    recalled = await memory_service.recall_relevant(None, actor, "deployment region europe")
    assert recalled == []


@pytest.mark.asyncio
async def test_another_users_private_memory_is_never_recalled(store):
    """THE core isolation test: an identical query from a different user must
    not surface user A's private memory."""
    organization_id = uuid.uuid4()
    alice = _user(organization_id)
    bob = _user(organization_id)

    await memory_service.create_memory(
        None,
        alice,
        MemoryCreate(scope="user", memory_type="fact", content="database pool sizing"),
    )

    alice_recall = await memory_service.recall_relevant(None, alice, "database pool")
    bob_recall = await memory_service.recall_relevant(None, bob, "database pool")

    assert len(alice_recall) == 1
    assert bob_recall == [], "semantic similarity must not grant visibility"


@pytest.mark.asyncio
async def test_cross_organization_memory_is_never_recalled(store):
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    user_a = _user(org_a)
    user_b = _user(org_b)

    await memory_service.create_memory(
        None, user_a, MemoryCreate(scope="user", memory_type="fact", content="database pool")
    )
    assert await memory_service.recall_relevant(None, user_b, "database pool") == []


@pytest.mark.asyncio
async def test_recall_limit_is_respected(store, monkeypatch):
    actor = _user(uuid.uuid4())
    for i in range(10):
        await memory_service.create_memory(
            None,
            actor,
            MemoryCreate(scope="user", memory_type="fact", content=f"database pool note {i}"),
        )

    settings = memory_service.get_settings()
    monkeypatch.setattr(settings, "memory_recall_limit", 3)
    recalled = await memory_service.recall_relevant(None, actor, "database pool")
    assert len(recalled) <= 3


@pytest.mark.asyncio
async def test_recall_limit_of_zero_disables_memory_entirely(store, monkeypatch):
    actor = _user(uuid.uuid4())
    await memory_service.create_memory(
        None, actor, MemoryCreate(scope="user", memory_type="fact", content="database pool")
    )
    settings = memory_service.get_settings()
    monkeypatch.setattr(settings, "memory_recall_limit", 0)
    assert await memory_service.recall_relevant(None, actor, "database pool") == []


@pytest.mark.asyncio
async def test_character_budget_caps_injected_memory(store, monkeypatch):
    actor = _user(uuid.uuid4())
    for i in range(5):
        await memory_service.create_memory(
            None,
            actor,
            MemoryCreate(
                scope="user", memory_type="fact", content=f"database pool {'x' * 100} {i}"
            ),
        )

    settings = memory_service.get_settings()
    monkeypatch.setattr(settings, "memory_context_char_budget", 250)
    recalled = await memory_service.recall_relevant(None, actor, "database pool")
    assert sum(len(m.content) for m in recalled) <= 250


@pytest.mark.asyncio
async def test_recall_records_access(store):
    actor = _user(uuid.uuid4())
    await memory_service.create_memory(
        None, actor, MemoryCreate(scope="user", memory_type="fact", content="database pool")
    )
    recalled = await memory_service.recall_relevant(None, actor, "database pool")
    assert store["touched"] == [m.id for m in recalled]


# --- update ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_changes_content_and_is_reflected_in_recall(store):
    actor = _user(uuid.uuid4())
    memory = await memory_service.create_memory(
        None, actor, MemoryCreate(scope="user", memory_type="fact", content="billing invoice")
    )
    # Not recallable for a deployment query before the edit.
    assert await memory_service.recall_relevant(None, actor, "deployment region") == []

    await memory_service.update_memory(
        None, actor, memory.id, MemoryUpdate(content="deployment region europe")
    )

    recalled = await memory_service.recall_relevant(None, actor, "deployment region")
    assert len(recalled) == 1
    assert "deployment region" in recalled[0].content


@pytest.mark.asyncio
async def test_update_of_another_users_memory_is_not_found(store):
    organization_id = uuid.uuid4()
    alice, bob = _user(organization_id), _user(organization_id)
    memory = await memory_service.create_memory(
        None, alice, MemoryCreate(scope="user", memory_type="fact", content="database pool")
    )
    with pytest.raises(NotFoundError):
        await memory_service.update_memory(None, bob, memory.id, MemoryUpdate(content="hijacked"))


# --- supersession ---------------------------------------------------------


@pytest.mark.asyncio
async def test_superseding_retires_the_old_memory_and_keeps_provenance(store):
    actor = _user(uuid.uuid4())
    old = await memory_service.create_memory(
        None,
        actor,
        MemoryCreate(scope="user", memory_type="fact", content="deployment region europe"),
    )
    new = await memory_service.create_memory(
        None,
        actor,
        MemoryCreate(
            scope="user",
            memory_type="fact",
            content="deployment region europe and database pool",
            supersedes_memory_id=old.id,
        ),
    )

    assert new.supersedes_memory_id == old.id
    recalled = await memory_service.recall_relevant(None, actor, "deployment region")
    ids = {m.id for m in recalled}
    assert new.id in ids
    assert old.id not in ids, "a superseded memory must not be recalled"


@pytest.mark.asyncio
async def test_cannot_supersede_a_memory_you_cannot_see(store):
    organization_id = uuid.uuid4()
    alice, bob = _user(organization_id), _user(organization_id)
    alices = await memory_service.create_memory(
        None, alice, MemoryCreate(scope="user", memory_type="fact", content="database pool")
    )
    with pytest.raises(NotFoundError):
        await memory_service.create_memory(
            None,
            bob,
            MemoryCreate(
                scope="user",
                memory_type="fact",
                content="database pool mine now",
                supersedes_memory_id=alices.id,
            ),
        )


# --- deletion (the required test) -----------------------------------------


@pytest.mark.asyncio
async def test_deleted_memory_is_unrecallable_and_its_embedding_destroyed(store):
    """The mandatory lifecycle test:

        memory exists -> is recallable -> deleted
        -> derived embedding removed -> no longer recallable
    """
    actor = _user(uuid.uuid4())
    memory = await memory_service.create_memory(
        None, actor, MemoryCreate(scope="user", memory_type="fact", content="database pool sizing")
    )

    # 1. exists and is recallable
    before = await memory_service.recall_relevant(None, actor, "database pool")
    assert [m.id for m in before] == [memory.id]

    # 2. delete
    assert await memory_service.delete_memory(None, actor, memory.id) is True

    # 3. the derived embedding and content are destroyed, not just hidden
    row = next(r for r in store["rows"] if r.id == memory.id)
    assert row.status == "deleted"
    assert row.content == ""
    assert all(v == 0.0 for v in row.embedding), "embedding must be destroyed, not retained"

    # 4. no longer recallable
    assert await memory_service.recall_relevant(None, actor, "database pool") == []
    # 5. and no longer directly readable
    with pytest.raises(NotFoundError):
        await memory_service.get_memory(None, actor, memory.id)


@pytest.mark.asyncio
async def test_repeated_deletion_is_safe_and_observable(store):
    actor = _user(uuid.uuid4())
    memory = await memory_service.create_memory(
        None, actor, MemoryCreate(scope="user", memory_type="fact", content="database pool")
    )
    assert await memory_service.delete_memory(None, actor, memory.id) is True
    # Second and third calls are no-ops reporting False, never errors.
    assert await memory_service.delete_memory(None, actor, memory.id) is False
    assert await memory_service.delete_memory(None, actor, memory.id) is False


@pytest.mark.asyncio
async def test_deleting_a_nonexistent_memory_raises_not_found(store):
    actor = _user(uuid.uuid4())
    with pytest.raises(NotFoundError):
        await memory_service.delete_memory(None, actor, uuid.uuid4())


@pytest.mark.asyncio
async def test_cannot_delete_another_users_memory(store):
    organization_id = uuid.uuid4()
    alice, bob = _user(organization_id), _user(organization_id)
    memory = await memory_service.create_memory(
        None, alice, MemoryCreate(scope="user", memory_type="fact", content="database pool")
    )
    with pytest.raises(NotFoundError):
        await memory_service.delete_memory(None, bob, memory.id)
    # Still intact for its owner.
    assert await memory_service.get_memory(None, alice, memory.id)


# --- context formatting ---------------------------------------------------


def test_format_memory_context_is_empty_for_no_memories():
    """An empty selection must produce no injected text at all, so prompts
    stay byte-identical to pre-memory behavior."""
    assert memory_service.format_memory_context([]) == ""


def test_format_memory_context_labels_notes_and_omits_citation_markers():
    memories = [
        RecalledMemory(
            id=uuid.uuid4(),
            scope="user",
            memory_type="preference",
            content="Prefers metric units",
            distance=0.1,
        )
    ]
    rendered = memory_service.format_memory_context(memories)
    assert "Previously saved notes" in rendered
    assert "Prefers metric units" in rendered
    # No bracketed numbers -- memory must never look like a citable source.
    assert "[1]" not in rendered


def test_recalled_memory_relevance_is_inverse_of_distance():
    memory = RecalledMemory(
        id=uuid.uuid4(), scope="user", memory_type="fact", content="x", distance=0.25
    )
    assert memory.relevance == pytest.approx(0.75)
