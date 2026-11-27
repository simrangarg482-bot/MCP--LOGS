"""`agents.feedback.service` -- authorization, validation and idempotency of
outcome feedback, with the repository and audit trail replaced by in-memory
fakes (the database-level guarantees are covered by
`tests/database/test_answer_feedback_migration_postgres.py`)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.agents.feedback import service
from app.agents.feedback.schemas import FeedbackSubmit
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.shared.schemas import ActorKind, Identity

_NOW = datetime(2026, 11, 28, tzinfo=UTC)


def _user(organization_id: uuid.UUID, *, permissions: frozenset[str] = frozenset()) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        permissions=permissions,
    )


def _execution(owner: Identity, **overrides):
    values = dict(
        id=uuid.uuid4(),
        organization_id=owner.organization_id,
        user_id=owner.user_id,
        agent_name="answer_question",
        status="succeeded",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeStore:
    """In-memory stand-in for `agents.feedback.repository`, keyed the way the
    real unique constraint is: (execution_id, user_id)."""

    def __init__(self, executions=()):
        self.executions = {execution.id: execution for execution in executions}
        self.rows: dict[tuple[uuid.UUID, uuid.UUID], SimpleNamespace] = {}
        self.audit: list[dict] = []
        self.upserts = 0

    async def get_execution(self, session, *, organization_id, execution_id):
        execution = self.executions.get(execution_id)
        # Mirrors RLS + the explicit organization filter.
        if execution is None or execution.organization_id != organization_id:
            return None
        return execution

    async def get_feedback(self, session, *, organization_id, execution_id, user_id):
        row = self.rows.get((execution_id, user_id))
        return row if row is not None and row.organization_id == organization_id else None

    async def upsert_feedback(
        self, session, *, organization_id, execution_id, user_id, rating, reason_code, comment
    ):
        self.upserts += 1
        key = (execution_id, user_id)
        existing = self.rows.get(key)
        row = SimpleNamespace(
            id=existing.id if existing else uuid.uuid4(),
            organization_id=organization_id,
            execution_id=execution_id,
            user_id=user_id,
            rating=rating,
            reason_code=reason_code,
            comment=comment,
            created_at=existing.created_at if existing else _NOW,
            updated_at=_NOW,
        )
        self.rows[key] = row
        return row


@pytest.fixture()
def store(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr(service.repository, "get_execution", fake.get_execution)
    monkeypatch.setattr(service.repository, "get_feedback", fake.get_feedback)
    monkeypatch.setattr(service.repository, "upsert_feedback", fake.upsert_feedback)

    async def fake_audit(session, actor, action, resource_type, resource_id=None, metadata=None):
        fake.audit.append({"action": action, "resource_id": resource_id, "metadata": metadata})

    monkeypatch.setattr(service, "record_audit_event", fake_audit)
    return fake


async def _submit(actor, execution_id, **body):
    return await service.submit_feedback(None, actor, execution_id, FeedbackSubmit(**body))


# --- the happy paths -------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("rating", ["helpful", "not_helpful"])
async def test_valid_execution_accepts_either_rating(store, rating) -> None:
    actor = _user(uuid.uuid4())
    execution = _execution(actor)
    store.executions[execution.id] = execution

    feedback = await _submit(actor, execution.id, rating=rating)

    assert feedback.rating == rating
    # Linked to the exact execution, not to anything derived from the question.
    assert feedback.execution_id == execution.id
    assert feedback.comment is None and feedback.reason_code is None


@pytest.mark.asyncio
async def test_explanation_is_optional_not_required_even_when_negative(store) -> None:
    actor = _user(uuid.uuid4())
    execution = _execution(actor)
    store.executions[execution.id] = execution

    assert (await _submit(actor, execution.id, rating="not_helpful")).comment is None


@pytest.mark.asyncio
async def test_reason_and_comment_are_stored_for_a_negative_rating(store) -> None:
    actor = _user(uuid.uuid4())
    execution = _execution(actor)
    store.executions[execution.id] = execution

    feedback = await _submit(
        actor, execution.id, rating="not_helpful", reason_code="outdated", comment="  v1 docs  "
    )

    assert feedback.reason_code == "outdated"
    assert feedback.comment == "v1 docs"  # trimmed by the schema


@pytest.mark.asyncio
async def test_reason_code_is_dropped_from_a_helpful_rating(store) -> None:
    actor = _user(uuid.uuid4())
    execution = _execution(actor)
    store.executions[execution.id] = execution

    feedback = await _submit(actor, execution.id, rating="helpful", reason_code="incorrect")

    assert feedback.reason_code is None


@pytest.mark.asyncio
async def test_triage_executions_can_be_rated_too(store) -> None:
    actor = _user(uuid.uuid4())
    execution = _execution(actor, agent_name="triage_incident")
    store.executions[execution.id] = execution

    assert (await _submit(actor, execution.id, rating="helpful")).rating == "helpful"


# --- one rating per user per execution -------------------------------------


@pytest.mark.asyncio
async def test_changing_a_rating_updates_in_place_and_is_audited_as_an_update(store) -> None:
    actor = _user(uuid.uuid4())
    execution = _execution(actor)
    store.executions[execution.id] = execution

    first = await _submit(actor, execution.id, rating="helpful")
    second = await _submit(actor, execution.id, rating="not_helpful", reason_code="incorrect")

    assert second.id == first.id  # same row, not a second one
    assert len(store.rows) == 1
    assert second.rating == "not_helpful"
    assert [event["action"] for event in store.audit] == [
        "answer_feedback.submit",
        "answer_feedback.update",
    ]


@pytest.mark.asyncio
async def test_resubmitting_an_identical_rating_is_a_noop(store) -> None:
    actor = _user(uuid.uuid4())
    execution = _execution(actor)
    store.executions[execution.id] = execution

    await _submit(actor, execution.id, rating="helpful")
    await _submit(actor, execution.id, rating="helpful")  # a double click

    assert store.upserts == 1
    assert len(store.audit) == 1


@pytest.mark.asyncio
async def test_repeated_questions_are_separate_executions_with_separate_feedback(store) -> None:
    actor = _user(uuid.uuid4())
    first, second = _execution(actor), _execution(actor)
    store.executions.update({first.id: first, second.id: second})

    await _submit(actor, first.id, rating="helpful")
    await _submit(actor, second.id, rating="not_helpful")

    assert store.rows[(first.id, actor.user_id)].rating == "helpful"
    assert store.rows[(second.id, actor.user_id)].rating == "not_helpful"


# --- authorization & tenancy ----------------------------------------------


@pytest.mark.asyncio
async def test_unknown_execution_is_not_found(store) -> None:
    with pytest.raises(NotFoundError):
        await _submit(_user(uuid.uuid4()), uuid.uuid4(), rating="helpful")


@pytest.mark.asyncio
async def test_another_organizations_execution_is_indistinguishable_from_missing(store) -> None:
    victim = _user(uuid.uuid4())
    attacker = _user(uuid.uuid4())
    execution = _execution(victim)
    store.executions[execution.id] = execution

    with pytest.raises(NotFoundError) as exc_info:
        await _submit(attacker, execution.id, rating="not_helpful")

    assert exc_info.value.error_code == "feedback.execution_not_found"
    assert store.rows == {}


@pytest.mark.asyncio
async def test_a_colleagues_execution_in_the_same_organization_cannot_be_rated(store) -> None:
    organization_id = uuid.uuid4()
    asker = _user(organization_id)
    colleague = _user(organization_id)
    execution = _execution(asker)
    store.executions[execution.id] = execution

    with pytest.raises(NotFoundError):
        await _submit(colleague, execution.id, rating="not_helpful")

    assert store.rows == {}


@pytest.mark.asyncio
async def test_executions_without_a_user_cannot_be_rated(store) -> None:
    """MCP and scheduled runs have `user_id = NULL`."""
    actor = _user(uuid.uuid4())
    execution = _execution(actor, user_id=None)
    store.executions[execution.id] = execution

    with pytest.raises(NotFoundError):
        await _submit(actor, execution.id, rating="helpful")


@pytest.mark.asyncio
async def test_non_answer_agents_cannot_be_rated(store) -> None:
    actor = _user(uuid.uuid4())
    execution = _execution(actor, agent_name="knowledge_gap_agent")
    store.executions[execution.id] = execution

    with pytest.raises(NotFoundError):
        await _submit(actor, execution.id, rating="helpful")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "failed"])
async def test_only_succeeded_executions_can_be_rated(store, status) -> None:
    actor = _user(uuid.uuid4())
    execution = _execution(actor, status=status)
    store.executions[execution.id] = execution

    with pytest.raises(ConflictError) as exc_info:
        await _submit(actor, execution.id, rating="helpful")

    assert exc_info.value.error_code == "feedback.execution_not_rateable"


@pytest.mark.asyncio
async def test_a_non_user_identity_cannot_rate(store) -> None:
    agent = Identity.for_agent("some_agent", uuid.uuid4())

    with pytest.raises(PermissionDeniedError):
        await _submit(agent, uuid.uuid4(), rating="helpful")


@pytest.mark.asyncio
async def test_get_my_feedback_never_returns_someone_elses_rating(store) -> None:
    organization_id = uuid.uuid4()
    author, other = _user(organization_id), _user(organization_id)
    execution = _execution(author)
    store.executions[execution.id] = execution
    await _submit(author, execution.id, rating="not_helpful")

    assert (await service.get_my_feedback(None, author, execution.id)).rating == "not_helpful"
    assert await service.get_my_feedback(None, other, execution.id) is None


# --- secrets and audit hygiene --------------------------------------------


@pytest.mark.asyncio
async def test_secrets_in_the_comment_are_redacted_before_storage(store) -> None:
    actor = _user(uuid.uuid4())
    execution = _execution(actor)
    store.executions[execution.id] = execution
    secret = "ghp_" + "b" * 36

    feedback = await _submit(
        actor, execution.id, rating="not_helpful", comment=f"it told me to use {secret} - wrong"
    )

    assert secret not in feedback.comment
    assert "[REDACTED:github_token]" in feedback.comment
    assert secret not in store.rows[(execution.id, actor.user_id)].comment


@pytest.mark.asyncio
async def test_audit_event_never_contains_the_comment_text(store) -> None:
    actor = _user(uuid.uuid4())
    execution = _execution(actor)
    store.executions[execution.id] = execution

    await _submit(
        actor,
        execution.id,
        rating="not_helpful",
        comment="private words " + "ghp_" + "c" * 36,
    )

    (event,) = store.audit
    assert event["metadata"]["has_comment"] is True
    assert event["metadata"]["comment_redactions"] == 1
    assert "private words" not in repr(event)
    assert "ghp_" not in repr(event)


@pytest.mark.asyncio
async def test_comment_is_capped_even_if_redaction_lengthens_it(store) -> None:
    actor = _user(uuid.uuid4())
    execution = _execution(actor)
    store.executions[execution.id] = execution
    # Each 20-char `api_key=xxxxxxxx` becomes a longer marker; 100 of them overflow 2000.
    comment = " ".join(["api_key=abcdefgh"] * 100)[:2000]

    feedback = await _submit(actor, execution.id, rating="not_helpful", comment=comment)

    assert len(feedback.comment) <= 2000


# --- insights --------------------------------------------------------------


@pytest.mark.asyncio
async def test_insights_require_the_knowledge_review_permission() -> None:
    with pytest.raises(PermissionDeniedError):
        await service.get_insights(None, _user(uuid.uuid4()))


# --- schema validation -----------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"rating": "great"},
        {"rating": "helpful", "reason_code": "because"},
        {"rating": "helpful", "comment": "x" * 2001},
        {"rating": "helpful", "organization_id": str(uuid.uuid4())},
    ],
)
def test_invalid_bodies_are_rejected_by_the_schema(body) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FeedbackSubmit(**body)


def test_blank_comment_becomes_none() -> None:
    assert FeedbackSubmit(rating="helpful", comment="   \n ").comment is None
