"""`90ff736ced55` (batch 4.6 orphaned-branch cleanup) must be idempotent.

WHY THIS EXISTS
    That migration only exists to undo drift the never-merged
    `origin/simran-ekip` branch left on one specific database. Its first
    draft removed those objects unconditionally
    (`ALTER TABLE eval_runs ...`, `op.drop_column('agent_executions', ...)`),
    so `alembic upgrade head` against any database that branch never touched
    -- a freshly created CI/local one included -- crashed with
    `UndefinedTable` / `UndefinedColumn`, and the migration chain could not
    be run from scratch at all (2026-09-02 MCP follow-up audit, section 7).

    Same static-scan approach as `test_migration_coverage.py`: running the
    migration needs a live Postgres, which would push this out of the
    offline suite -- exactly the property that let the original problem
    through. This asserts on the *constructs used* instead.
"""

from __future__ import annotations

from pathlib import Path

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "database"
    / "migrations"
    / "versions"
    / "90ff736ced55_batch_4_6_remove_orphaned_simran_ekip_.py"
)


def _upgrade_source() -> str:
    text = _MIGRATION.read_text(encoding="utf-8")
    start = text.index("def upgrade(")
    end = text.index("def downgrade(")
    return text[start:end]


def test_migration_file_is_discoverable() -> None:
    assert _MIGRATION.is_file(), f"migration not found at {_MIGRATION}"


def test_upgrade_drops_columns_only_with_if_exists() -> None:
    upgrade = _upgrade_source()
    # `op.drop_column` has no `IF EXISTS`; it must go through raw SQL.
    assert "op.drop_column(" not in upgrade, (
        "op.drop_column() is unconditional -- a column that only ever existed "
        "on the origin/simran-ekip branch makes this crash on a fresh database. "
        "Use `ALTER TABLE ... DROP COLUMN IF EXISTS`."
    )
    assert "DROP COLUMN IF EXISTS" in upgrade


def test_upgrade_guards_the_rls_alters_behind_an_existence_check() -> None:
    upgrade = _upgrade_source()
    # The eval_runs/eval_case_results RLS toggles have no `IF EXISTS` form --
    # they must sit inside a `to_regclass(...) IS NOT NULL` guard so they are
    # a no-op on a database that never had those tables.
    assert "ROW LEVEL SECURITY" in upgrade
    assert "to_regclass(" in upgrade and "IS NOT NULL" in upgrade, (
        "RLS ALTERs on eval_runs/eval_case_results are not guarded by an "
        "existence check -- `alembic upgrade head` crashes on a database that "
        "never had those tables."
    )


def test_upgrade_drops_tables_and_indexes_with_if_exists() -> None:
    upgrade = _upgrade_source()
    for line in upgrade.splitlines():
        if "op.drop_table(" in line or "op.drop_index(" in line:
            assert "if_exists=True" in line, f"unconditional drop: {line.strip()}"
