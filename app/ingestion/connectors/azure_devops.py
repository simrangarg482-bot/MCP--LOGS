"""Azure DevOps connector -- the third of Milestone 9's remaining connectors
(PROJECT_PLAN.md section 14), following `jira.py`/`teams.py`.

Implements `app.ingestion.connectors.base.Connector` structurally (a
`Protocol` -- see that module's docstring for why no explicit inheritance is
used). Fetches *work items* (bugs, tasks, user stories -- Azure DevOps'
umbrella term for all of these) via WIQL (Work Item Query Language), Azure
DevOps' own SQL-like query syntax, the documented way to list work items
matching a filter before fetching their full field data in a second,
separate batch call.

Expected `ResolvedConnectorConfig.config` shape for this source:
    {"organization": "acme-corp", "projects": ["ProjA", "ProjB"]}
`organization` is the Azure DevOps organization slug (`https://dev.azure.com
/<organization>/`); `projects` is a list of project *names* (Azure DevOps
work item IDs are organization-wide unique regardless of project, unlike
Jira's per-project issue keys, so -- unlike `JiraConnector` -- there is no
separate per-project key namespace to worry about colliding; project names
are still needed to scope each WIQL query and to build a human-readable
`source_url`).

`config.credential_ref` is expected to be a literal Azure DevOps Personal
Access Token (PAT) -- Azure DevOps' REST API accepts a PAT as the password
half of HTTP Basic auth with an empty username, so this connector builds
that header itself the same way `JiraConnector` builds a Basic-auth header
from its own literal credential shape. Same flagged "literal value until
`shared/security` exists" placeholder every other connector's docstring
carries.

Two-phase-per-project fetch, unlike every other connector in this codebase:
WIQL only returns matching work item IDs (no field data), so a project's
full ID list is fetched fresh on every `fetch_batch` call (cheap -- IDs
only, no field payload) and then sliced into `_BATCH_SIZE`-sized ID batches,
each resolved to full field data via a separate `POST .../workitemsbatch`
call. This mirrors `GitHubConnector._list_tree_page`'s "cheap to re-fetch
the whole listing every page, given this connector's stateless-between-
calls design" precedent exactly, just applied to WIQL's IDs-then-batch-fetch
shape instead of a git tree.

`since` is applied as a real server-side WIQL filter (`[System.ChangedDate]
>= '...'`) -- unlike `TeamsConnector`'s Graph API gap, WIQL supports this
directly, so there is no client-side-filter fallback needed here.

Comments are fetched per work item (`GET .../workItems/{id}/comments`, a
preview-only endpoint -- see `_COMMENTS_API_VERSION`) and appended to
`content` after a `"--- Comments ---"` delimiter, the same
`GitHubConnector`/`JiraConnector` precedent -- skipped when
`System.CommentCount == 0`.
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

_API_VERSION = "7.1"
# The work-item-comments REST API is preview-only as of this API version
# family (unlike every other endpoint this connector calls, which is
# stable at `_API_VERSION`) -- a separate constant so that distinction
# stays visible at each call site rather than silently reusing the stable
# version string for an endpoint that doesn't actually support it.
_COMMENTS_API_VERSION = "7.1-preview.3"
_BATCH_SIZE = 200  # Azure DevOps' documented max ids per workitemsbatch call.
_FIELDS = [
    "System.Title",
    "System.Description",
    "System.WorkItemType",
    "System.State",
    "System.AssignedTo",
    "System.CreatedDate",
    "System.ChangedDate",
    "System.CommentCount",
]


@dataclass
class _AzureDevOpsClient:
    """What `AzureDevOpsConnector.authenticate` returns as
    `AuthenticatedClient`. Bundles the authenticated HTTP client with the
    fixed `organization`/`projects` list `fetch_batch` needs -- same
    reasoning as `_SlackClient`/`_GitHubClient`/`_JiraClient`/`_TeamsClient`.
    """

    http: httpx.AsyncClient
    organization: str
    projects: list[str]


class AzureDevOpsConnector:
    """Fetches work items from a fixed set of Azure DevOps projects within
    one organization per `connector_config`.
    """

    source_name = "azure_devops"
    # Azure DevOps' REST API documents a per-organization request limit in
    # "TSTUs" (a weighted-cost unit, not a flat requests/second figure) --
    # same "no single authoritative flat number" situation `JiraConnector`/
    # `TeamsConnector` already document for their own sources. 2.0 req/s is
    # a conservative steady-state budget for the WIQL + batch-fetch calls
    # this connector makes, matching section 4.5's "a conservative constant
    # is fine" allowance.
    requests_per_second = 2.0

    async def authenticate(self, config: ResolvedConnectorConfig) -> _AzureDevOpsClient:
        """Build an authenticated Azure DevOps client from `config`.

        `config.credential_ref` is treated as a literal PAT -- see module
        docstring -- base64-encoded here (with an empty username) into a
        Basic-auth header. Calls `GET _apis/projects` once so an invalid/
        revoked PAT or wrong `organization` fails loudly here rather than on
        the first real fetch, matching every other connector's
        `authenticate`-time verification call.
        """
        organization = config.config.get("organization", "")
        if not organization:
            raise RuntimeError("Azure DevOps connector config is missing required 'organization'")

        encoded_credentials = base64.b64encode(f":{config.credential_ref}".encode("utf-8")).decode(
            "ascii"
        )
        http = httpx.AsyncClient(
            base_url=f"https://dev.azure.com/{organization}/",
            headers={
                "Authorization": f"Basic {encoded_credentials}",
                "Accept": "application/json",
            },
            timeout=30.0,
        )
        try:
            response = await http.get("_apis/projects", params={"api-version": _API_VERSION})
            response.raise_for_status()
        except Exception:
            await http.aclose()
            logger.warning(
                "azure_devops_authenticate_failed",
                connector_config_id=str(config.connector_config_id),
            )
            raise

        projects = list(config.config.get("projects", []))
        logger.info(
            "azure_devops_authenticate_succeeded",
            connector_config_id=str(config.connector_config_id),
            project_count=len(projects),
        )
        return _AzureDevOpsClient(http=http, organization=organization, projects=projects)

    async def fetch_batch(
        self,
        client: _AzureDevOpsClient,
        *,
        since: datetime | None,
        cursor: str | None,
    ) -> FetchResult:
        """Fetch one batch of raw Azure DevOps work items from
        `client.projects`.

        `cursor` is this connector's own opaque JSON envelope
        `{"project_index": int, "batch_start": int}` -- `batch_start` is an
        index into the *current* WIQL result for this project (re-fetched
        fresh every call, see module docstring), not a server-issued token.

        Each raw item is Azure DevOps' work item dict with `"_project"`
        injected (needed by `normalize`, which only ever sees one
        self-contained `raw_item` at a time).
        """
        project_index, batch_start = self._decode_cursor(cursor)

        if project_index >= len(client.projects):
            return FetchResult(items=[], next_cursor=None, has_more=False)

        project = client.projects[project_index]
        work_item_ids = await self._query_work_item_ids(client.http, project, since)

        batch_ids = work_item_ids[batch_start : batch_start + _BATCH_SIZE]
        items: list[dict[str, Any]] = []
        if batch_ids:
            items = await self._fetch_work_items(client.http, batch_ids)
            for item in items:
                item["_project"] = project
                item["_organization"] = client.organization
                comment_count = (item.get("fields") or {}).get("System.CommentCount", 0)
                item["_comments_text"] = (
                    await self._fetch_comments_text(client.http, project, item["id"])
                    if comment_count
                    else ""
                )

        next_batch_start = batch_start + len(batch_ids)
        project_exhausted = next_batch_start >= len(work_item_ids)

        if not project_exhausted:
            next_state = {"project_index": project_index, "batch_start": next_batch_start}
            has_more = True
        else:
            next_project_index = project_index + 1
            next_state = {"project_index": next_project_index, "batch_start": 0}
            has_more = next_project_index < len(client.projects)

        return FetchResult(
            items=items,
            next_cursor=json.dumps(next_state) if has_more else None,
            has_more=has_more,
        )

    async def _query_work_item_ids(
        self, http: httpx.AsyncClient, project: str, since: datetime | None
    ) -> list[int]:
        """Run a WIQL query scoped to `project`, returning matching work item
        IDs in `System.ChangedDate` order (oldest first, so pagination is
        stable across the batches within one sync run).
        """
        wiql = "SELECT [System.Id] FROM WorkItems"
        if since is not None:
            since_str = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            wiql += f" WHERE [System.ChangedDate] >= '{since_str}'"
        wiql += " ORDER BY [System.ChangedDate] ASC"

        response = await http.post(
            f"{project}/_apis/wit/wiql",
            params={"api-version": _API_VERSION},
            json={"query": wiql},
        )
        response.raise_for_status()
        payload = response.json()
        return [entry["id"] for entry in payload.get("workItems", [])]

    async def _fetch_work_items(
        self, http: httpx.AsyncClient, ids: list[int]
    ) -> list[dict[str, Any]]:
        """Resolve a batch of work item IDs (<= `_BATCH_SIZE`) to full field
        data via a single `workitemsbatch` call.
        """
        response = await http.post(
            "_apis/wit/workitemsbatch",
            params={"api-version": _API_VERSION},
            json={"ids": ids, "fields": _FIELDS},
        )
        response.raise_for_status()
        payload = response.json()
        return list(payload.get("value", []))

    async def _fetch_comments_text(
        self, http: httpx.AsyncClient, project: str, work_item_id: int
    ) -> str:
        """Concatenate every comment on work item `work_item_id` into one
        text block (`"author: text"` per comment, blank-line separated) --
        appended into `content` by `normalize`. Skipped entirely by the
        caller when `System.CommentCount == 0`, the same "avoid a wasted
        call for the common case" gate `JiraConnector._fetch_comments_text`
        and `GitHubConnector._fetch_issue_comments_text` already use.
        """
        response = await http.get(
            f"{project}/_apis/wit/workItems/{work_item_id}/comments",
            params={"api-version": _COMMENTS_API_VERSION},
        )
        response.raise_for_status()
        payload = response.json()
        comments: list[dict[str, Any]] = payload.get("comments", [])
        return "\n\n".join(
            f"{(comment.get('createdBy') or {}).get('displayName', 'unknown')}: "
            f"{comment.get('text', '')}"
            for comment in comments
        )

    def normalize(self, raw_item: Any) -> RawDocument:
        """Convert one raw Azure DevOps work item dict (with `"_project"`/
        `"_organization"` injected by `fetch_batch`) into a `RawDocument`.

        `raw_item["url"]` (Azure DevOps' own field) is the *API* URL
        (`.../_apis/wit/workItems/{id}`), not a human-navigable one -- unlike
        `JiraConnector.normalize`'s `self` link (which points at the right
        host and just needs a path swap), Azure DevOps' `url` has no simple
        transform into a browse link, so the browse URL is built directly
        from `_organization`/`_project`/`id` instead (why both were injected
        onto `raw_item` by `fetch_batch`, not just `_project`).
        """
        project = raw_item["_project"]
        organization = raw_item["_organization"]
        work_item_id = raw_item["id"]
        fields = raw_item.get("fields", {})
        title = fields.get("System.Title") or ""
        description = fields.get("System.Description") or ""
        content = f"{title}\n\n{description}" if description else title
        comments_text = raw_item.get("_comments_text") or ""
        if comments_text:
            content = f"{content}\n\n--- Comments ---\n\n{comments_text}"

        metadata: dict[str, str] = {"project": project}
        work_item_type = fields.get("System.WorkItemType")
        if work_item_type:
            metadata["work_item_type"] = work_item_type
        state = fields.get("System.State")
        if state:
            metadata["state"] = state
        assigned_to = (fields.get("System.AssignedTo") or {}).get("displayName")
        if assigned_to:
            metadata["assigned_to"] = assigned_to
        if fields.get("System.CreatedDate"):
            metadata["created"] = fields["System.CreatedDate"]
        if fields.get("System.ChangedDate"):
            metadata["updated"] = fields["System.ChangedDate"]
        comment_count = fields.get("System.CommentCount")
        if comment_count is not None:
            metadata["comments_count"] = str(comment_count)

        source_url = f"https://dev.azure.com/{organization}/{project}/_workitems/edit/{work_item_id}"

        return RawDocument(
            source=self.source_name,
            external_id=f"{project}:{work_item_id}",
            content=content,
            title=title or None,
            source_url=source_url,
            metadata=metadata,
        )

    async def close(self, client: _AzureDevOpsClient) -> None:
        """Close the underlying `httpx.AsyncClient` opened by `authenticate`."""
        await client.http.aclose()

    @staticmethod
    def _decode_cursor(cursor: str | None) -> tuple[int, int]:
        """Parse this connector's opaque cursor envelope back into
        `(project_index, batch_start)`, defaulting to the first project at
        `batch_start=0` when `cursor` is None (a full sync's first page, or
        an incremental sync's first page for this run).
        """
        if cursor is None:
            return 0, 0
        state = json.loads(cursor)
        return int(state["project_index"]), int(state["batch_start"])
