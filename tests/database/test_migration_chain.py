"""The migration history is one linear chain and the lifecycle migrations sit in the intended order. No database."""

from __future__ import annotations

import pathlib

from alembic.config import Config
from alembic.script import ScriptDirectory

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _script() -> ScriptDirectory:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "app" / "database" / "migrations"))
    return ScriptDirectory.from_config(cfg)


def test_there_is_exactly_one_head_and_it_is_the_answer_feedback_migration() -> None:
    assert _script().get_heads() == ["d3b7f1a9c5e2"]


def test_the_answer_feedback_migration_sits_directly_on_the_retention_migration() -> None:
    assert _script().get_revision("d3b7f1a9c5e2").down_revision == "e1c7a9d3b5f2"


def test_the_lifecycle_migrations_are_chained_in_order_on_the_password_reset_migration() -> None:
    script = _script()
    chain = ["b2c5e8a1d4f7", "d4a7b1c8e3f6", "e5b8c2d9f4a7", "f7c1d3a9b2e5", "a8e3c5d1f2b7", "b9f4d6e2a1c8", "c1a5e7f3b9d2", "d9b2f4a7c1e3", "e1c7a9d3b5f2"]
    for earlier, later in zip(chain, chain[1:]):
        assert script.get_revision(later).down_revision == earlier


def test_the_history_is_linear_no_merges_no_branches() -> None:
    script = _script()
    revisions = list(script.walk_revisions())
    assert all(not isinstance(r.down_revision, tuple) for r in revisions)  # no merge points
    children: dict[str, int] = {}
    for r in revisions:
        if isinstance(r.down_revision, str):
            children[r.down_revision] = children.get(r.down_revision, 0) + 1
    assert all(count == 1 for count in children.values())  # no revision has two children (no branch)
