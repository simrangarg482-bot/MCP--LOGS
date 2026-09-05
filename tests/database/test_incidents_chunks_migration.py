"""Audit finding 6: static checks on
`d4e5f6a7b8c9_incidents_chunks.py` -- the migration creating the new
`incidents_chunks` table. Same static-scan approach as
`test_migration_coverage.py`/`test_migration_idempotency.py`: running the
migration needs a live Postgres, out of reach of the offline suite, so
these assert on the constructs used in the file instead.
"""

from __future__ import annotations

from pathlib import Path

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "database"
    / "migrations"
    / "versions"
    / "d4e5f6a7b8c9_incidents_chunks.py"
)


def _upgrade_source() -> str:
    text = _MIGRATION.read_text(encoding="utf-8")
    start = text.index("def upgrade(")
    end = text.index("def downgrade(")
    return text[start:end]


def test_migration_file_is_discoverable() -> None:
    assert _MIGRATION.is_file(), f"migration not found at {_MIGRATION}"


def test_upgrade_creates_the_incidents_chunks_table() -> None:
    upgrade = _upgrade_source()
    assert "op.create_table('incidents_chunks'" in upgrade


def test_upgrade_enables_and_forces_row_level_security() -> None:
    """Requirement 4/7 and requirement 9's "tenant isolation is preserved"
    -- same `ENABLE` + `FORCE` pairing every other `<collection>_chunks`
    table's RLS migration uses (`c7d4e8f19a2b`), enabled in this same
    migration rather than a follow-up one, so the table is never RLS-less
    even transiently.
    """
    upgrade = _upgrade_source()
    assert "ALTER TABLE incidents_chunks ENABLE ROW LEVEL SECURITY" in upgrade
    assert "ALTER TABLE incidents_chunks FORCE ROW LEVEL SECURITY" in upgrade


def test_upgrade_creates_a_tenant_isolation_policy_scoped_to_organization_id() -> None:
    # The policy name is a module-level constant (`_POLICY_NAME`), not a
    # string literal inside `upgrade()` itself -- check the whole file, not
    # just the `upgrade()` slice, for that one assertion.
    full_text = _MIGRATION.read_text(encoding="utf-8")
    upgrade = _upgrade_source()

    assert "_POLICY_NAME = 'tenant_isolation'" in full_text
    assert "CREATE POLICY" in upgrade
    assert "ON incidents_chunks" in upgrade
    # Same direct-column compare every other `_DIRECT_TABLES` entry in
    # `c7d4e8f19a2b` uses -- no join-scoped policy needed, since this table
    # (like every other chunk table) has its own `organization_id` column.
    assert "organization_id = current_setting(" in upgrade


def test_downgrade_reverses_rls_before_dropping_the_table() -> None:
    text = _MIGRATION.read_text(encoding="utf-8")
    downgrade = text[text.index("def downgrade("):]
    assert "DROP POLICY IF EXISTS" in downgrade
    assert "DISABLE ROW LEVEL SECURITY" in downgrade
    assert "op.drop_table('incidents_chunks')" in downgrade
