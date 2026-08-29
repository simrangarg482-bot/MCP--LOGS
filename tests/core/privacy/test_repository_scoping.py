"""Tests for `app.core.privacy.repository`'s SQL -- specifically that every
mutation is scoped by BOTH `user_id` and `organization_id`.

These compile each statement to SQL text and assert on the WHERE clause,
rather than executing against a database (this suite has no Postgres -- see
`docs/PROJECT_STATUS.md`). That is a real limitation and worth stating: it
proves the predicate is *present in the statement*, not that Postgres
enforces it as expected. It is nonetheless the check that matters most here,
because the failure mode being guarded against is a forgotten predicate --
a cross-tenant `DELETE` that a mocked service-layer test cannot detect at
all, since the mock never sees the SQL.

`project_memberships` gets extra attention: it is the one table with no
`organization_id` column of its own, so its scoping has to come from a
`projects` subquery, and a regression there would silently delete another
organization's memberships.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Delete, Update

from app.core.privacy import repository


def _compile(stmt) -> str:
    """Render a statement as literal SQL for inspection."""
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def _contains_uuid(sql: str, value: uuid.UUID) -> bool:
    """Whether `sql` references `value`.

    Checks both renderings: SQLAlchemy's literal-bind output for a Postgres
    UUID column is the dash-less hex form, while a UUID appearing inside a
    string value (e.g. the anonymized-email placeholder) keeps its dashes.
    """
    return value.hex in sql.replace("-", "") or str(value) in sql


class _CapturingSession:
    """Captures the statement passed to `execute` instead of running it."""

    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        return _FakeResult()

    async def get(self, model, pk):  # pragma: no cover - not used by mutation tests
        return None


class _FakeResult:
    rowcount = 0

    def scalar_one(self):
        return 0


_MUTATIONS = [
    "delete_refresh_tokens",
    "delete_user_roles",
    "delete_project_memberships",
    "delete_external_identity_mappings",
    "anonymize_agent_executions",
]


@pytest.mark.parametrize("function_name", _MUTATIONS)
@pytest.mark.asyncio
async def test_every_user_scoped_mutation_filters_by_user_and_organization(function_name):
    session = _CapturingSession()
    user_id = uuid.uuid4()
    organization_id = uuid.uuid4()

    await getattr(repository, function_name)(session, user_id, organization_id)

    assert len(session.statements) == 1
    sql = _compile(session.statements[0])
    assert _contains_uuid(sql, user_id), f"{function_name} is not scoped by user_id"
    assert _contains_uuid(
        sql, organization_id
    ), f"{function_name} is not scoped by organization_id"


@pytest.mark.asyncio
async def test_project_membership_deletion_is_scoped_through_projects_subquery():
    """The table has no organization_id, so isolation depends entirely on
    this subquery. Assert the join to `projects` is really there."""
    session = _CapturingSession()
    organization_id = uuid.uuid4()
    await repository.delete_project_memberships(session, uuid.uuid4(), organization_id)

    sql = _compile(session.statements[0])
    assert "projects" in sql.lower()
    assert "organization_id" in sql.lower()
    assert _contains_uuid(sql, organization_id)


@pytest.mark.asyncio
async def test_agent_execution_anonymization_nulls_user_id_and_keeps_the_row():
    """Must be an UPDATE ... SET user_id = NULL, never a DELETE -- the row is
    organization-level telemetry that outlives the user."""
    session = _CapturingSession()
    await repository.anonymize_agent_executions(session, uuid.uuid4(), uuid.uuid4())

    statement = session.statements[0]
    # Asserted on the statement TYPE, not on a substring of the rendered
    # SQL: a value like the anonymized-email placeholder legitimately
    # contains the text "delete", so string-sniffing here would be both
    # fragile and misleading.
    assert isinstance(statement, Update)
    assert not isinstance(statement, Delete)
    assert "user_id=null" in _compile(statement).lower().replace(" ", "")


@pytest.mark.asyncio
async def test_user_record_anonymization_clears_pii_and_deactivates():
    session = _CapturingSession()
    user_id = uuid.uuid4()
    await repository.anonymize_user_record(session, user_id)

    statement = session.statements[0]
    assert isinstance(statement, Update)  # never a DELETE -- RESTRICT FKs forbid it
    sql = _compile(statement)
    lowered = sql.lower().replace(" ", "")
    # Email replaced with the deterministic placeholder, name replaced,
    # password hash nulled, account deactivated.
    assert repository.anonymized_email_for(user_id) in sql
    assert repository.ANONYMIZED_DISPLAY_NAME in sql
    assert "password_hash=null" in lowered
    assert "is_active=false" in lowered


@pytest.mark.asyncio
async def test_invitation_anonymization_replaces_email_only():
    session = _CapturingSession()
    placeholder = "deleted-user-x@deleted.invalid"
    await repository.anonymize_invitations_for_email(
        session, "person@example.com", uuid.uuid4(), placeholder
    )

    statement = session.statements[0]
    # The row itself survives -- it is partly an audit record of who invited
    # whom -- so this must be an UPDATE, never a DELETE.
    assert isinstance(statement, Update)
    assert placeholder in _compile(statement)


# --- anonymized-identifier helpers ----------------------------------------


def test_anonymized_email_is_deterministic_for_idempotency():
    """Anonymizing twice must produce the same address -- otherwise a retry
    would create a second distinct placeholder."""
    user_id = uuid.uuid4()
    assert repository.anonymized_email_for(user_id) == repository.anonymized_email_for(user_id)


def test_anonymized_email_is_unique_per_user():
    """`users.email` is UNIQUE NOT NULL, so two deleted users must not
    collide on the placeholder."""
    assert repository.anonymized_email_for(uuid.uuid4()) != repository.anonymized_email_for(
        uuid.uuid4()
    )


def test_anonymized_email_uses_a_reserved_undeliverable_domain():
    """RFC 2606 reserves `.invalid`; a placeholder must never be able to
    receive mail or be mistaken for a real address."""
    assert repository.anonymized_email_for(uuid.uuid4()).endswith("@deleted.invalid")


def test_is_anonymized_email_recognizes_the_placeholder():
    user_id = uuid.uuid4()
    assert repository.is_anonymized_email(repository.anonymized_email_for(user_id))


def test_is_anonymized_email_rejects_real_addresses_and_none():
    assert not repository.is_anonymized_email("person@example.com")
    assert not repository.is_anonymized_email(None)
    assert not repository.is_anonymized_email("")
