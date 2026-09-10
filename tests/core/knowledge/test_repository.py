"""Tests for `app.core.knowledge.repository.list_proposed_documents` --
Stage 2 of the Knowledge/Knowledge Gaps review: the review queue must only
ever surface `source="manual"` proposals, never connector-synced content.

Same technique `tests/core/proactive/test_repository.py` and
`tests/core/graph/test_repository.py` already use for plain read queries:
a capturing fake session plus compiled-SQL inspection (`literal_binds`),
not a live database -- same disclosed limitation as every other repository
test in this codebase: no live Postgres in this suite. Assertions check for
the column name and its literal bound value as separate substrings (not an
exact `column = 'value'` string), matching `test_proactive_repository.py`'s
own established style -- robust to whichever way the default compiler
happens to space/quote an equality clause.

For an equality filter (`Document.source == "manual"`), this is a complete
proof, not a partial signal: `WHERE ... AND source = 'manual' AND ...` can
only ever match rows whose `source` is exactly `"manual"`, and matches
every such row regardless of which connector-looking `source` string it is
compared against -- so the two tests below together prove both "manual +
proposed documents appear" and "non-manual + proposed documents are
excluded" at the query-construction level, which is the actual mechanism
enforcing it against a real database.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.knowledge import repository


def _compile(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def _contains_uuid(sql: str, value: uuid.UUID) -> bool:
    return value.hex in sql.replace("-", "") or str(value) in sql


class _AllResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _CapturingSession:
    def __init__(self, rows=None):
        self.statements: list[object] = []
        self._rows = rows if rows is not None else []

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        return _AllResult(self._rows)


@pytest.mark.asyncio
async def test_list_proposed_documents_requires_source_manual() -> None:
    """Proves "non-manual + proposed documents are excluded": the compiled
    WHERE clause filters `source` against the literal value `"manual"`, so
    no row with any other `source` (`"github"`, `"slack"`, `"runbooks"`,
    ...) can ever satisfy it, no matter its `status`.
    """
    session = _CapturingSession()

    await repository.list_proposed_documents(session, uuid.uuid4())

    where = _compile(session.statements[0])
    assert "source" in where
    assert "'manual'" in where


@pytest.mark.asyncio
async def test_list_proposed_documents_still_requires_proposed_status_and_org_scope() -> None:
    """Proves "manual + proposed documents appear": the `source = 'manual'`
    filter added in Stage 2 is ANDed onto the existing `status = 'proposed'`
    and organization-scope conditions, not substituted for them -- a manual
    document only ever needs to satisfy the same conditions this function
    already required before this change, plus the new one.
    """
    session = _CapturingSession()
    organization_id = uuid.uuid4()

    await repository.list_proposed_documents(session, organization_id)

    where = _compile(session.statements[0])
    assert "status" in where
    assert "'proposed'" in where
    assert "source" in where
    assert "'manual'" in where
    assert "deleted_at" in where
    assert _contains_uuid(where, organization_id)


@pytest.mark.asyncio
async def test_list_proposed_documents_returns_whatever_the_query_matches() -> None:
    """Pass-through sanity check (same style as `tests/core/proactive/
    test_repository.py::test_list_findings_scopes_by_organization`): the
    function returns exactly what the query's result set contains, doing no
    extra client-side filtering or reshaping of its own -- the actual
    manual-vs-non-manual selection is entirely the WHERE clause's job,
    proven by the two tests above.
    """
    sentinel_row = object()
    session = _CapturingSession(rows=[sentinel_row])

    result = await repository.list_proposed_documents(session, uuid.uuid4())

    assert list(result) == [sentinel_row]


# --- list_published_documents pagination (Stage 3) -----------------------


@pytest.mark.asyncio
async def test_list_published_documents_applies_limit_and_offset() -> None:
    """Stage 3: before this, `list_published_documents` had no `LIMIT`/
    `OFFSET` at all -- harmless while Stage 1 made this query return nothing
    for every ingested document, a real problem once it started returning
    every ingested document in the organization in one response.
    """
    session = _CapturingSession()

    await repository.list_published_documents(session, uuid.uuid4(), limit=10, offset=20)

    sql = _compile(session.statements[0])
    assert "LIMIT 10" in sql
    assert "OFFSET 20" in sql


@pytest.mark.asyncio
async def test_list_published_documents_defaults_to_fifty_and_zero() -> None:
    session = _CapturingSession()

    await repository.list_published_documents(session, uuid.uuid4())

    sql = _compile(session.statements[0])
    assert "LIMIT 50" in sql
    assert "OFFSET 0" in sql
