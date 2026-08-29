"""Every ORM-mapped table must actually be created by a migration.

WHY THIS EXISTS
    `mcp_requests` shipped as a fully-formed SQLAlchemy model
    (`app.database.models.mcp_models.McpRequest`) that no migration ever
    created. Nothing caught it: the model imported fine, unit tests that
    touch `core.observability` monkeypatch the repository layer, and the
    `/observability/mcp` router test fakes `get_mcp_dashboard` outright --
    so the whole stack was green while any real database that had only ever
    run `alembic upgrade head` was missing the table entirely. Every read
    and write against it failed with `UndefinedTableError`, and
    `GET /observability/mcp` returned HTTP 500 in production.

    This test targets the ROOT CAUSE -- "a model exists with no migration to
    create it" -- rather than that one table, so the next model added
    without a migration fails here immediately instead of in production.

HOW IT CHECKS
    Model side: import the same model modules
    `app/database/migrations/base.py` imports (that module cannot be
    imported directly here -- it reads `alembic.context.config` at import
    time and only works inside an Alembic run), then read
    `Base.metadata.tables`.

    Migration side: statically scan `versions/*.py` for `op.create_table('x')`.
    Deliberately static rather than executing the migrations: running them
    needs a live database, which would make this an integration test and put
    it out of reach of the ordinary offline suite -- exactly the property
    that let the original bug through.
"""

from __future__ import annotations

import re
from pathlib import Path

# Registers every mapped table on `Base.metadata` as a side effect -- the
# same set of imports `app/database/migrations/base.py` performs.
from app.database.models import agent_models  # noqa: F401
from app.database.models import auth_models  # noqa: F401
from app.database.models import core_models  # noqa: F401
from app.database.models import ingestion_models  # noqa: F401
from app.database.models import mcp_models  # noqa: F401
from app.database.models import memory_models  # noqa: F401
from app.database.models import graph_models  # noqa: F401
from app.database.models import pattern_models  # noqa: F401
from app.database.models import retrieval_models  # noqa: F401
from app.database.models import tenancy_models  # noqa: F401
from app.database.session import Base

_VERSIONS_DIR = Path(__file__).resolve().parents[2] / "app" / "database" / "migrations" / "versions"
_CREATE_TABLE = re.compile(r"""op\.create_table\(\s*['"]([a-zA-Z_][a-zA-Z0-9_]*)['"]""")


def _tables_created_by_migrations() -> set[str]:
    created: set[str] = set()
    for path in _VERSIONS_DIR.glob("*.py"):
        created.update(_CREATE_TABLE.findall(path.read_text(encoding="utf-8")))
    return created


def test_migration_directory_is_discoverable() -> None:
    """Guards the test itself: if the versions directory ever moves, the
    coverage assertion below would silently pass against an empty set.
    """
    assert _VERSIONS_DIR.is_dir(), f"migration versions dir not found at {_VERSIONS_DIR}"
    assert _tables_created_by_migrations(), "no op.create_table() calls found -- scan is broken"


def test_every_model_table_has_a_creating_migration() -> None:
    model_tables = set(Base.metadata.tables)
    migration_tables = _tables_created_by_migrations()

    missing = sorted(model_tables - migration_tables)
    assert not missing, (
        "These tables are mapped by a SQLAlchemy model but no migration creates them, so "
        f"`alembic upgrade head` produces a database without them: {missing}. "
        "Add a migration with op.create_table() for each."
    )


def test_mcp_requests_specifically_is_created_by_a_migration() -> None:
    """The specific regression. Kept alongside the general check so the
    original failure is named explicitly and cannot silently come back if
    the general check is ever weakened.
    """
    assert mcp_models.McpRequest.__tablename__ == "mcp_requests"
    assert "mcp_requests" in _tables_created_by_migrations()
