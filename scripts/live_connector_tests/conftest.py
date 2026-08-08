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
