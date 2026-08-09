"""Shared pytest fixtures for EKIP's live connector integration tests.

WHY THESE TESTS LIVE UNDER `scripts/`, NOT `tests/`
    `pyproject.toml` sets `testpaths = ["tests"]`, so a bare `pytest` (what
    CI, or a developer running the full suite, would normally type) auto-
    collects everything under `tests/`. These tests make REAL network calls
    to REAL external services using REAL credentials -- auto-collecting
    them would mean an ordinary `pytest` run could hang, fail, or make
    unwanted live API calls on any machine that happens to have (or lack)
    connector credentials configured. This project has already made this
    exact call once before: `scripts/test_connectors.py`'s own docstring
    states it lives in `scripts/`, not `tests/`, for precisely this reason
    ("not a pytest test... hits real external APIs with real credentials").
    These files are pytest-shaped (so they get proper PASS/FAIL reporting,
    fixtures, and `-k`/`-v` filtering) but follow that same placement
    convention -- run them explicitly by path, they will not run by
    accident:

        pytest scripts/live_connector_tests/ -v -s

CREDENTIALS -- REUSED, NOT DUPLICATED
    These tests load credentials from `tests/ingestion_retrieval/.env` via
    that directory's own `config.py` (`ConnectorSpec` per connector) --
    the same `.env` already used by `tests/ingestion_retrieval/
    test_connectors.py`. No new `.env` file is introduced here; if you've
    already filled in `tests/ingestion_retrieval/.env`, these tests use it
    as-is. Any connector left blank there is SKIPPED here too (via
    `pytest.skip`, printed clearly), never FAILED.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_HARNESS_DIR = _PROJECT_ROOT / "tests" / "ingestion_retrieval"
for _path in (_PROJECT_ROOT, _HARNESS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import pytest  # noqa: E402

import config as harness_config  # noqa: E402  (tests/ingestion_retrieval/config.py)
import utils as harness_utils  # noqa: E402  (tests/ingestion_retrieval/utils.py)


@pytest.fixture(autouse=True)
async def _fresh_engine_pool_per_event_loop():
    """Drop the app engine's pooled connections before each async test.

    WHY THIS EXISTS
        `app.database.session.engine` is a module-level singleton with a
        normal connection pool, and an asyncpg connection is permanently
        bound to the event loop that opened it. Two things in this suite run
        on *different* loops:

          1. the session-scoped `organization_id` fixture below, which
             bootstraps via `harness_utils.run_async` -> that harness's own
             persistent `_shared_loop`; and
          2. every `async def test_`, which `pytest-asyncio` (auto mode, per
             `pyproject.toml`) runs on a fresh loop of its own.

        So the bootstrap fills the pool with connections bound to the
        harness loop, and the first database-touching test then borrows one
        of those from a different loop -- `RuntimeError: Task got Future
        attached to a different loop`, followed by `Event loop is closed`
        during teardown. Only the runbooks connector hit this, because it is
        the only connector that talks to the database at all; Slack/GitHub/
        Jira use httpx, which opens a fresh connection per loop and is
        therefore immune.

        Disposing here (an async autouse fixture, so it runs *inside* the
        same loop as the test that follows it) guarantees the test opens its
        own connections on its own loop. The cost is one new connection per
        test, which is irrelevant for a suite that is already making real
        network calls to external APIs.

    WHY NOT FIX IT IN THE APP INSTEAD
        Nothing is wrong with the application here. A single long-lived
        engine on a single loop is exactly right for the API server and the
        arq workers. The mismatch is created purely by this suite running
        two different loops in one process, so the fix belongs here.
    """
    from app.database.session import engine

    await engine.dispose()
    yield


@pytest.fixture(scope="session")
def cfg() -> harness_config.Config:
    return harness_config.load_config()


@pytest.fixture(scope="session")
def organization_id(cfg: harness_config.Config) -> uuid.UUID:
    """One real, bootstrapped test organization, shared across every live
    connector test in this whole pytest session. Bootstrapping calls
    EKIP's real service functions directly (no REST self-signup endpoint
    exists) -- see `tests/ingestion_retrieval/utils.py`'s module docstring,
    point 1, for the full explanation; reused here rather than re-solved a
    third time.
    """
    identity = harness_utils.bootstrap_admin_sync(
        org_name=cfg.org_name,
        org_slug=cfg.org_slug,
        email=cfg.admin_email,
        display_name=cfg.admin_display_name,
    )
    return uuid.UUID(identity["organization_id"])


def _require_connector(source: str):
    @pytest.fixture(scope="session")
    def _fixture(cfg: harness_config.Config) -> harness_config.ConnectorSpec:
        spec = cfg.connectors[source]
        if not spec.available:
            pytest.skip(f"{source}: {spec.unavailable_reason}")
        return spec

    return _fixture


# One skip-if-unconfigured fixture per connector -- each test file below
# takes the one(s) it needs as a normal pytest fixture argument.
slack_spec = _require_connector("slack")
github_spec = _require_connector("github")
jira_spec = _require_connector("jira")
confluence_spec = _require_connector("confluence")
teams_spec = _require_connector("teams")
sharepoint_spec = _require_connector("sharepoint")
azure_devops_spec = _require_connector("azure_devops")
runbooks_spec = _require_connector("runbooks")
