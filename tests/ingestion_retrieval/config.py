"""Configuration for the ingestion & retrieval RAG test harness.

This is a STANDALONE config, separate from the project's own
`app.shared.config.settings.Settings`. It reads two things:

1. This harness's own `.env` (`tests/ingestion_retrieval/.env`, copy from
   `.env.example` in this same directory) -- external-source credentials
   (Slack/GitHub/Jira/etc.) and harness-only knobs (base URL, org name).
   These deliberately do NOT live in the project's own `.env`/`Settings`:
   `app/shared/config/settings.py`'s own docstring states connector
   credentials are "deliberately NOT included" there, scoped per-source
   instead (see `app/ingestion/connectors/`). This harness follows the same
   `EKIP_TEST_<SOURCE>_...` naming convention already established by the
   real, existing `scripts/test_connectors.py` (which covers Slack + GitHub
   only) -- this harness's `.env.example` extends that same convention to
   the other five external connectors.

2. The project's OWN `.env`, indirectly -- by importing
   `app.shared.config.settings.get_settings()` for `database_url` and
   `openai_api_key`, which the ingestion/retrieval/RAG pipeline genuinely
   needs (a real Postgres with the `vector` extension, and a real OpenAI key
   for query rewriting / answer generation / grounding's LLM-escalation
   path). This harness never redefines or overrides those -- it reads the
   real, unmodified `Settings` object exactly as the running application
   does.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError as exc:  # pragma: no cover - guidance path, not logic
    raise SystemExit(
        "Missing dependency 'python-dotenv'. Install it into the PROJECT's "
        "own virtualenv (this harness imports app.* modules directly, so it "
        "must run in that same environment):\n"
        "    pip install python-dotenv httpx"
    ) from exc

_HARNESS_DIR = Path(__file__).resolve().parent
_ENV_PATH = _HARNESS_DIR / ".env"
load_dotenv(_ENV_PATH, override=False)


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _csv(name: str) -> list[str]:
    raw = _get(name, "")
    return [part.strip() for part in raw.split(",") if part.strip()] if raw else []


@dataclass(frozen=True)
class ConnectorSpec:
    """What one connector needs to actually run: whether creds are present,
    the plaintext `credential_ref` to hand `ResolvedConnectorConfig`, and the
    source-specific `config` dict each connector's own `authenticate`/
    `fetch_batch` expects (shapes verified by reading each connector file
    directly -- see README.md's per-connector table).
    """

    source: str
    available: bool
    credential_ref: str | None
    config: dict
    unavailable_reason: str = ""


def _slack_spec() -> ConnectorSpec:
    token = _get("EKIP_TEST_SLACK_BOT_TOKEN")
    channel_ids = _csv("EKIP_TEST_SLACK_CHANNEL_IDS")
    if not token or not channel_ids:
        return ConnectorSpec(
            "slack", False, None, {}, "EKIP_TEST_SLACK_BOT_TOKEN / EKIP_TEST_SLACK_CHANNEL_IDS not set"
        )
    return ConnectorSpec("slack", True, token, {"channels": channel_ids})


def _github_spec() -> ConnectorSpec:
    token = _get("EKIP_TEST_GITHUB_TOKEN")
    repo = _get("EKIP_TEST_GITHUB_REPO")
    ref = _get("EKIP_TEST_GITHUB_REF", "main")
    if not token or not repo:
        return ConnectorSpec("github", False, None, {}, "EKIP_TEST_GITHUB_TOKEN / EKIP_TEST_GITHUB_REPO not set")
    return ConnectorSpec("github", True, token, {"repos": [{"repo": repo, "ref": ref}]})


def _jira_spec() -> ConnectorSpec:
    base_url = _get("EKIP_TEST_JIRA_BASE_URL")
    email = _get("EKIP_TEST_JIRA_EMAIL")
    api_token = _get("EKIP_TEST_JIRA_API_TOKEN")
    projects = _csv("EKIP_TEST_JIRA_PROJECTS")
    if not (base_url and email and api_token and projects):
        return ConnectorSpec(
            "jira",
            False,
            None,
            {},
            "EKIP_TEST_JIRA_BASE_URL / EKIP_TEST_JIRA_EMAIL / EKIP_TEST_JIRA_API_TOKEN / EKIP_TEST_JIRA_PROJECTS not set",
        )
    return ConnectorSpec("jira", True, f"{email}:{api_token}", {"base_url": base_url, "projects": projects})


def _confluence_spec() -> ConnectorSpec:
    base_url = _get("EKIP_TEST_CONFLUENCE_BASE_URL")
    email = _get("EKIP_TEST_CONFLUENCE_EMAIL")
    api_token = _get("EKIP_TEST_CONFLUENCE_API_TOKEN")
    spaces = _csv("EKIP_TEST_CONFLUENCE_SPACES")
    if not (base_url and email and api_token and spaces):
        return ConnectorSpec(
            "confluence",
            False,
            None,
            {},
            "EKIP_TEST_CONFLUENCE_BASE_URL / EKIP_TEST_CONFLUENCE_EMAIL / EKIP_TEST_CONFLUENCE_API_TOKEN / "
            "EKIP_TEST_CONFLUENCE_SPACES not set",
        )
    return ConnectorSpec(
        "confluence", True, f"{email}:{api_token}", {"base_url": base_url, "spaces": spaces}
    )


def _teams_spec() -> ConnectorSpec:
    access_token = _get("EKIP_TEST_TEAMS_ACCESS_TOKEN")
    team_id = _get("EKIP_TEST_TEAMS_TEAM_ID")
    channels = _csv("EKIP_TEST_TEAMS_CHANNEL_IDS")
    if not (access_token and team_id and channels):
        return ConnectorSpec(
            "teams",
            False,
            None,
            {},
            "EKIP_TEST_TEAMS_ACCESS_TOKEN / EKIP_TEST_TEAMS_TEAM_ID / EKIP_TEST_TEAMS_CHANNEL_IDS not set "
            "(this must be an ALREADY-ISSUED Microsoft Graph bearer access token -- this harness cannot "
            "run an interactive OAuth flow to mint one itself; see README.md)",
        )
    return ConnectorSpec("teams", True, access_token, {"team_id": team_id, "channels": channels})


def _sharepoint_spec() -> ConnectorSpec:
    access_token = _get("EKIP_TEST_SHAREPOINT_ACCESS_TOKEN")
    site_ids = _csv("EKIP_TEST_SHAREPOINT_SITE_IDS")
    if not (access_token and site_ids):
        return ConnectorSpec(
            "sharepoint",
            False,
            None,
            {},
            "EKIP_TEST_SHAREPOINT_ACCESS_TOKEN / EKIP_TEST_SHAREPOINT_SITE_IDS not set (same already-issued "
            "Graph bearer token caveat as Teams -- see README.md)",
        )
    return ConnectorSpec("sharepoint", True, access_token, {"site_ids": site_ids})


def _azure_devops_spec() -> ConnectorSpec:
    org = _get("EKIP_TEST_AZURE_DEVOPS_ORG")
    pat = _get("EKIP_TEST_AZURE_DEVOPS_PAT")
    projects = _csv("EKIP_TEST_AZURE_DEVOPS_PROJECTS")
    if not (org and pat and projects):
        return ConnectorSpec(
            "azure_devops",
            False,
            None,
            {},
            "EKIP_TEST_AZURE_DEVOPS_ORG / EKIP_TEST_AZURE_DEVOPS_PAT / EKIP_TEST_AZURE_DEVOPS_PROJECTS not set",
        )
    return ConnectorSpec("azure_devops", True, pat, {"organization": org, "projects": projects})


def _runbooks_spec() -> ConnectorSpec:
    # No external credential at all -- RunbooksConnector reads real
    # postmortem rows straight out of this project's own Postgres via
    # core.incidents.service.list_postmortems_for_ingestion. "Available" here
    # means "a database connection is configured," not "credentials are
    # set" -- there simply are none for this source.
    return ConnectorSpec("runbooks", True, "unused", {})


@dataclass(frozen=True)
class Config:
    base_url: str
    request_timeout_seconds: float
    org_name: str
    org_slug: str
    admin_email: str
    admin_display_name: str
    connectors: dict[str, ConnectorSpec] = field(default_factory=dict)


def load_config() -> Config:
    connectors = {
        spec.source: spec
        for spec in (
            _slack_spec(),
            _github_spec(),
            _jira_spec(),
            _confluence_spec(),
            _teams_spec(),
            _sharepoint_spec(),
            _azure_devops_spec(),
            _runbooks_spec(),
        )
    }
    return Config(
        base_url=_get("EKIP_BASE_URL", "http://localhost:8000").rstrip("/"),
        request_timeout_seconds=float(_get("EKIP_TEST_REQUEST_TIMEOUT_SECONDS", "45")),
        org_name=_get("EKIP_TEST_ORG_NAME", "RAG Pipeline Test Corp"),
        org_slug=_get("EKIP_TEST_ORG_SLUG", "rag-pipeline-test"),
        admin_email=_get("EKIP_TEST_ADMIN_EMAIL", "rag-harness-admin@example.test"),
        admin_display_name=_get("EKIP_TEST_ADMIN_NAME", "RAG Harness Admin"),
        connectors=connectors,
    )
