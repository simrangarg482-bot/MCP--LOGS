"""Read-only migration/schema drift diagnostic (Phase 3 Batch 4.5).

Answers one question safely: "does this database's stamped alembic revision
actually exist in this repository's migration history, and does it match the
repository's head?" -- without ever writing to the database. No `alembic
stamp`/`upgrade`/`downgrade` call anywhere in this file.

Why this exists: a real, observed incident (Batch 4.5) where the shared Neon
development database's `alembic_version` pointed at a revision id that
existed in neither `main` nor any other branch's migration history at all --
undetectable by `alembic current`/`alembic heads` alone, since both of those
commands only ever report what they're told, not whether it's internally
consistent. This script is the closest thing to an early-warning check for
that same failure mode recurring, meant to run before a real deployment's
migration step, not as a replacement for `alembic upgrade head` itself.

Deliberately conservative: it reports what it finds and stops. It does not
attempt any repair, migration, or stamp -- see
`docs/operations/migration-recovery.md` for why a guessed stamp is worse than
an honest "unresolved" here, and for the manual recovery procedure once a
human has reviewed this script's output.

Run: uv run python scripts/migration_status.py
"""

from __future__ import annotations

import asyncio
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.shared.config.settings import get_settings


def _script_directory() -> ScriptDirectory:
    cfg = Config("alembic.ini")
    return ScriptDirectory.from_config(cfg)


async def _current_db_revision() -> str | None:
    """A raw `SELECT version_num FROM alembic_version` -- deliberately not
    `alembic.runtime.migration.MigrationContext`, which would still work, but
    this is the simplest possible read-only query for the one column this
    script cares about, no engine/transaction machinery beyond what's needed
    to run it.
    """
    engine = create_async_engine(str(get_settings().database_url))
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            row = result.first()
            return row[0] if row else None
    finally:
        await engine.dispose()


def main() -> int:
    # Repo-only checks first -- no database connection needed, so these run
    # (and fail fast) even against a completely unreachable database, and
    # are exactly what a PR-time CI check can run without any live Postgres
    # at all (a broken/branched migration history is a defect in the
    # committed files, independent of any database's state).
    try:
        script = _script_directory()
        repo_heads = script.get_heads()
        all_revisions = {rev.revision for rev in script.walk_revisions()}
    except Exception as exc:
        # Alembic itself raises here for a broken down_revision chain (a
        # migration gap -- a file referencing a down_revision that doesn't
        # exist) -- surfaced as a clear failure instead of a raw traceback.
        print(f"Repository migration history: MIGRATION GAP -- {exc}")
        return 1

    if len(repo_heads) > 1:
        print(
            f"Repository migration history: MULTIPLE HEADS -- {', '.join(repo_heads)}. "
            "This means two migrations both claim the same down_revision (a real "
            "branch in the migration graph, exactly the shape of the main/"
            "origin/simran-ekip divergence this script was written to catch -- see "
            "docs/operations/migration-recovery.md). `alembic upgrade head` refuses "
            "to run with an ambiguous target until this is resolved with a real, "
            "reviewed `alembic merge` migration."
        )
        return 1

    try:
        db_revision = asyncio.run(_current_db_revision())
    except Exception as exc:
        print(f"Database revision:     UNREACHABLE ({exc})")
        print("Repository head(s):    " + ", ".join(repo_heads))
        print("Revision exists?       UNKNOWN -- could not query the database")
        print("Schema compatibility:  UNKNOWN")
        return 1

    revision_exists = db_revision in all_revisions if db_revision else False
    is_at_head = db_revision in repo_heads if db_revision else False

    print(f"Database revision:     {db_revision!r}")
    print(f"Repository head(s):    {', '.join(repo_heads)}")
    print(f"Revision exists?       {revision_exists}")

    if db_revision is None:
        print("Schema compatibility:  UNRESOLVED -- alembic_version table is empty")
        return 1
    if not revision_exists:
        print(
            "Schema compatibility:  UNRESOLVED -- this revision id is not present "
            "anywhere in app/database/migrations/versions/. Do NOT run `alembic "
            "stamp` to force this to a guessed value. See "
            "docs/operations/migration-recovery.md for the read-only investigation "
            "procedure and recovery options."
        )
        return 1
    if not is_at_head:
        print(
            "Schema compatibility:  BEHIND HEAD -- a real, known revision, but not "
            "the repository's current head. `alembic upgrade head` should be safe "
            "(each intermediate migration runs against a schema state it actually "
            "expects), but confirm via a disposable-database dry run first if this "
            "is a shared/production database."
        )
        return 0

    print("Schema compatibility:  OK -- database is at the repository's current head")
    return 0


if __name__ == "__main__":
    sys.exit(main())
