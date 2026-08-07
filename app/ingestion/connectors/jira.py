"""Jira connector -- the first of Milestone 9's remaining connectors
(PROJECT_PLAN.md section 14: "Additional connectors: Teams, Azure DevOps,
Jira, Confluence, SharePoint, runbooks/incident-report ingestion").

Implements `app.ingestion.connectors.base.Connector` structurally (a
`Protocol` -- see that module's docstring for why no explicit inheritance is
used). Follows `SlackConnector`/`GitHubConnector`'s established shape: a
small dataclass bundling the authenticated `httpx.AsyncClient` with whatever
per-config data `fetch_batch` needs, an opaque JSON-envelope cursor, and a
`normalize()` that only ever sees one self-contained raw item at a time.

Per PROJECT_PLAN.md section 4.4, Jira has no reliable webhook story in this
codebase's scope -- it is a **scheduled-polling-only** source, using
`since=last_successful_sync_at` for incremental syncs exactly like the
"Scheduled polling" bullet describes, with no separate webhook handler ever
built for it.

Unlike Slack/GitHub, a Jira Cloud instance's API base URL is tenant-specific
(`https://<your-domain>.atlassian.net`), not a fixed constant -- so, unlike
`_SLACK_API_BASE_URL`/`_GITHUB_API_BASE_URL`, this connector reads
`config.config["base_url"]` per `connector_config` rather than hardcoding one.

Expected `ResolvedConnectorConfig.config` shape for this source:
    {"base_url": "https://acme.atlassian.net", "projects": ["OPS", "ENG"]}
`projects` is a list of Jira project *keys* (not human-readable project
names), the same "IDs the IT Admin already knows, not names this connector
would have to resolve" choice `SlackConnector` made for channel IDs.

Uses Jira's REST API **v2**, not v3, deliberately: v3's `description`/
`comment` bodies are Atlassian Document Format (a structured JSON tree, not
plain text), which would need its own ADF-to-text renderer to be usable as
ingestible content -- out of scope for a first connector pass. v2 returns
`description` as a plain string, matching every other connector's
`RawDocument.content` expectation with no extra rendering step.

Comments are fetched per issue (`GET /rest/api/2/issue/{key}/comment`) and
appended to `content` after a `"--- Comments ---"` delimiter, the exact
precedent `GitHubConnector._fetch_issue_comments_text`/`_normalize_issue`
already establishes for the same "a ticket's discussion is part of its
searchable content, not a separate document" choice -- skipped per-issue
when `fields.comment.total == 0`, the same "avoid a wasted call for the
common case" gate GitHub's own `comments_count` check uses.

`config.credential_ref` is expected to hold `"<email>:<api_token>"` (Jira
Cloud's own documented Basic-auth credential pair, unencoded) -- this
connector base64-encodes it locally before use. Still a flagged placeholder
per `ResolvedConnectorConfig`'s docstring (no real secrets-store resolution
exists yet), just a different literal shape than Slack's/GitHub's bearer
tokens, since Jira Cloud's REST API requires Basic auth, not a bearer token,
for API-token-based access.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from app.ingestion.schemas import FetchResult, RawDocument, ResolvedConnectorConfig
from app.shared.config.logging import get_logger

logger = get_logger(__name__)

_SEARCH_PAGE_SIZE = 50  # Jira's default/typical maxResults page size.
_FIELDS = "summary,description,issuetype,status,assignee,reporter,created,updated,comment"


@dataclass
class _JiraClient:
    """What `JiraConnector.authenticate` returns as `AuthenticatedClient`.

    Bundles the authenticated HTTP client (already pointed at this tenant's
    own `base_url`) with the fixed project-key list from
    `ResolvedConnectorConfig.config["projects"]`, since `fetch_batch` itself
    only receives this object back, not the original config -- same
    reasoning as `_SlackClient`/`_GitHubClient`.
    """

    http: httpx.AsyncClient
    projects: list[str]
    base_url: str


class JiraConnector:
    """Fetches issues from a fixed set of Jira projects on one Jira Cloud
    instance per `connector_config` (a tenant may run more than one
    `connector_config` if it has multiple Jira sites).
    """

    source_name = "jira"
    # Jira Cloud's documented REST rate limit is cost-based, not a flat
    # requests/second figure, and varies by endpoint/plan -- there is no
    # single authoritative steady-state number the way GitHub publishes
    # 5,000/hour. 2.0 req/s is a conservative, commonly-safe budget for the
    # `search` endpoint specifically (the only endpoint this connector
    # calls), matching section 4.5's "a conservative constant is fine"
    # allowance for sources without a flat published ceiling.
    requests_per_second = 2.0

    async def authenticate(self, config: ResolvedConnectorConfig) -> _JiraClient:
        """Build an authenticated Jira client from `config`.

        `config.credential_ref` is treated as the literal `"<email>:<api_
        token>"` pair -- see module docstring -- base64-encoded here into a
        Basic-auth header. Calls `GET /rest/api/2/myself` once so a
        misconfigured/revoked token or wrong `base_url` fails loudly here
        rather than silently on the first `fetch_batch`, matching
        `SlackConnector.authenticate`'s `auth.test` precedent.
        """
        base_url = config.config.get("base_url", "").rstrip("/")
        if not base_url:
            raise RuntimeError("Jira connector config is missing required 'base_url'")

        encoded_credentials = base64.b64encode(config.credential_ref.encode("utf-8")).decode(
            "ascii"
        )
        http = httpx.AsyncClient(
            base_url=f"{base_url}/rest/api/2/",
            headers={
                "Authorization": f"Basic {encoded_credentials}",
                "Accept": "application/json",
            },
            timeout=30.0,
        )
        try:
            response = await http.get("myself")
            response.raise_for_status()
        except Exception:
            await http.aclose()
            logger.warning(
                "jira_authenticate_failed",
                connector_config_id=str(config.connector_config_id),
            )
            raise

        projects = list(config.config.get("projects", []))
        logger.info(
            "jira_authenticate_succeeded",
            connector_config_id=str(config.connector_config_id),
            project_count=len(projects),
        )
        return _JiraClient(http=http, projects=projects, base_url=base_url)

    async def fetch_batch(
        self,
        client: _JiraClient,
        *,
        since: datetime | None,
        cursor: str | None,
    ) -> FetchResult:
        """Fetch one page of raw Jira issues from `client.projects`.

        A `connector_config` can list multiple project keys, but Jira's
        `search` endpoint only ever searches one JQL query's results at a
        time -- `cursor` here is this connector's own opaque JSON envelope
        `{"project_index": int, "start_at": int}`, the same "resume
        mid-list, not just mid-page" shape `SlackConnector`'s
        `{"channel_index", "slack_cursor"}` cursor uses.

        Each raw item is Jira's issue dict with `"_project_key"` injected
        (needed by `normalize`, which only ever sees one self-contained
        `raw_item` at a time).
        """
        project_index, start_at = self._decode_cursor(cursor)

        if project_index >= len(client.projects):
            return FetchResult(items=[], next_cursor=None, has_more=False)

        project_key = client.projects[project_index]
        jql = f'project = "{project_key}"'
        if since is not None:
            since_str = since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
            jql += f' AND updated >= "{since_str}"'
        jql += " ORDER BY updated ASC"

        response = await client.http.get(
            "search",
            params={
                "jql": jql,
                "startAt": start_at,
                "maxResults": _SEARCH_PAGE_SIZE,
                "fields": _FIELDS,
            },
        )
        response.raise_for_status()
        payload = response.json()

        issues: list[dict[str, Any]] = payload.get("issues", [])
        for issue in issues:
            issue["_project_key"] = project_key
            comment_total = ((issue.get("fields") or {}).get("comment") or {}).get("total", 0)
            issue["_comments_text"] = (
                await self._fetch_comments_text(client.http, issue["key"]) if comment_total else ""
            )

        total = payload.get("total", 0)
        next_start_at = start_at + len(issues)
        project_exhausted = next_start_at >= total or not issues

        if not project_exhausted:
            next_state = {"project_index": project_index, "start_at": next_start_at}
            has_more = True
        else:
            next_project_index = project_index + 1
            next_state = {"project_index": next_project_index, "start_at": 0}
            has_more = next_project_index < len(client.projects)

        return FetchResult(
            items=issues,
            next_cursor=json.dumps(next_state) if has_more else None,
            has_more=has_more,
        )

    async def _fetch_comments_text(self, http: httpx.AsyncClient, issue_key: str) -> str:
        """Concatenate every comment on issue `issue_key` into one text
        block (`"author: body"` per comment, blank-line separated) --
        appended into `content` by `normalize`, the same
        `GitHubConnector._fetch_issue_comments_text` precedent this
        connector follows for the same reason: comments are part of a
        ticket's searchable content, not just a count.
        """
        response = await http.get(f"issue/{issue_key}/comment")
        response.raise_for_status()
        payload = response.json()
        comments: list[dict[str, Any]] = payload.get("comments", [])
        return "\n\n".join(
            f"{(comment.get('author') or {}).get('displayName', 'unknown')}: "
            f"{comment.get('body', '')}"
            for comment in comments
        )

    def normalize(self, raw_item: Any) -> RawDocument:
        """Convert one raw Jira issue dict (with `_project_key`/
        `_comments_text` injected by `fetch_batch`) into a `RawDocument`.
        """
        project_key = raw_item["_project_key"]
        key = raw_item["key"]
        fields = raw_item.get("fields", {})
        summary = fields.get("summary") or ""
        description = fields.get("description") or ""
        content = f"{summary}\n\n{description}" if description else summary
        comments_text = raw_item.get("_comments_text") or ""
        if comments_text:
            content = f"{content}\n\n--- Comments ---\n\n{comments_text}"

        metadata: dict[str, str] = {"project": project_key}
        comment_total = ((fields.get("comment")) or {}).get("total")
        if comment_total is not None:
            metadata["comments_count"] = str(comment_total)
        issue_type = (fields.get("issuetype") or {}).get("name")
        if issue_type:
            metadata["issue_type"] = issue_type
        status = (fields.get("status") or {}).get("name")
        if status:
            metadata["status"] = status
        assignee = (fields.get("assignee") or {}).get("displayName")
        if assignee:
            metadata["assignee"] = assignee
        reporter = (fields.get("reporter") or {}).get("displayName")
        if reporter:
            metadata["reporter"] = reporter
        if fields.get("created"):
            metadata["created"] = fields["created"]
        if fields.get("updated"):
            metadata["updated"] = fields["updated"]

        # `base_url` isn't carried on `raw_item` -- `fetch_batch` only injects
        # `_project_key`, since a `connector_config`'s `base_url` is fixed for
        # every item it ever produces. Reconstructing the browse URL from
        # `raw_item["self"]` (Jira's own REST-API self-link) avoids needing to
        # thread `base_url` through the raw item just for this one field --
        # `self` always points at the same host the issue was fetched from.
        api_self_url = raw_item.get("self", "")
        source_url = None
        if api_self_url:
            host = api_self_url.split("/rest/api/", 1)[0]
            source_url = f"{host}/browse/{key}"

        return RawDocument(
            source=self.source_name,
            external_id=key,
            content=content,
            title=summary or key,
            source_url=source_url,
            metadata=metadata,
        )

    async def close(self, client: _JiraClient) -> None:
        """Close the underlying `httpx.AsyncClient` opened by `authenticate`."""
        await client.http.aclose()

    @staticmethod
    def _decode_cursor(cursor: str | None) -> tuple[int, int]:
        """Parse this connector's opaque cursor envelope back into
        `(project_index, start_at)`, defaulting to the first project at
        `startAt=0` when `cursor` is None (a full sync's first page, or an
        incremental sync's first page for this run).
        """
        if cursor is None:
            return 0, 0
        state = json.loads(cursor)
        return int(state["project_index"]), int(state["start_at"])
