"""Confluence connector -- the fourth of Milestone 9's remaining connectors
(PROJECT_PLAN.md section 14), following `jira.py` closely: both are
Atlassian Cloud products sharing the same tenant-specific `base_url` and
Basic-auth-with-API-token credential shape.

Implements `app.ingestion.connectors.base.Connector` structurally (a
`Protocol` -- see that module's docstring for why no explicit inheritance is
used). Per PROJECT_PLAN.md section 4.4, Confluence has no reliable webhook
story in this codebase's scope -- like Jira, it is a
**scheduled-polling-only** source.

Expected `ResolvedConnectorConfig.config` shape for this source:
    {"base_url": "https://acme.atlassian.net", "spaces": ["ENG", "OPS"]}
`spaces` is a list of Confluence space *keys* (not display names) -- the
same "IDs the IT Admin already knows" choice every other multi-container
connector in this codebase (`SlackConnector`'s channels, `JiraConnector`'s
project keys, `AzureDevOpsConnector`'s projects) already makes.

Uses Confluence's `/content/search` endpoint with a CQL (Confluence Query
Language) filter, not the simpler `/content?spaceKey=...` endpoint --
deliberately, since CQL supports a real server-side `lastmodified >= ...`
clause for incremental sync (the same "use the real query language when a
real one exists" choice `JiraConnector` makes with JQL), where the simpler
endpoint has no equivalent filter at all.

Content is fetched as Confluence *storage format* (`body.storage.value` --
an XHTML-like markup, not Atlassian Document Format), matching
`JiraConnector`'s same "use the API version/expand that returns renderable
markup, not a structured tree that would need its own renderer" choice.
Storage-format HTML tags are stripped generically downstream by
`processors.cleaning.clean_content`, the same as `TeamsConnector`'s/
`AzureDevOpsConnector`'s HTML-ish content -- this connector does not strip
markup itself.

Fetches pages, blog posts, comments, and attachments in one CQL query
(`type in ("page","blogpost","comment","attachment")` -- CQL's `IN`
operator covers all four without a second call per space). Each item's
`metadata["kind"]` records which of the four it is, reusing the exact
`"kind"` metadata-key convention `GitHubConnector` already established for
its own commit/pull_request/issue distinction (see that module's
`_normalize_commit`/`_normalize_pull_request`/`_normalize_issue`) -- one
consistent place for downstream code to ask "what kind of item is this"
across sources. A comment's parent content id is threaded through as
`metadata["parent_id"]` when Confluence's response includes a `container`
reference for it.

Attachment *content* is downloaded via the attachment's own `_links.
download` path (resolved against `base_url` the same way `_links.webui`
already is for `source_url`) and extracted through the shared
`app.ingestion.office_extraction.extract_text` -- the exact module
`SharePointConnector`'s own Office/PDF/Excel support already uses, pulled
out specifically so this connector didn't have to duplicate that parsing
logic to close its own attachment-content gap. Only the same `.pdf`/
`.docx`/`.xlsx` set `office_extraction` supports gets real content; an
attachment with no download link, an unsupported extension, or a parse
failure is skipped (not erroring the whole batch), same "skipped, not an
error" contract every other per-item I/O step in this codebase already
follows.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from app.ingestion.office_extraction import extract_text
from app.ingestion.schemas import FetchResult, RawDocument, ResolvedConnectorConfig
from app.shared.config.logging import get_logger

logger = get_logger(__name__)

_SEARCH_PAGE_SIZE = 25  # Confluence's content search default page size.
# `container` is only meaningful for comment-type content (a comment's
# parent page/blog post) -- requesting it for every item is harmless no-op
# expansion for pages/blog posts, which have no `container` of their own.
_EXPAND = "body.storage,version,container"


@dataclass
class _ConfluenceClient:
    """What `ConfluenceConnector.authenticate` returns as
    `AuthenticatedClient`. Bundles the authenticated HTTP client with the
    fixed space-key list and `base_url` -- `fetch_batch`/`normalize` only
    ever receive this object back, same reasoning as `_JiraClient`.
    """

    http: httpx.AsyncClient
    spaces: list[str]
    base_url: str


class ConfluenceConnector:
    """Fetches pages, blog posts, and comments from a fixed set of
    Confluence spaces on one Confluence Cloud instance per `connector_config`.
    """

    source_name = "confluence"
    # Same situation `JiraConnector` documents for its own rate limit:
    # Atlassian Cloud's REST rate limiting is cost-based, not a single
    # published flat requests/second ceiling. 2.0 req/s is a conservative
    # steady-state budget for the `content/search` endpoint specifically.
    requests_per_second = 2.0

    async def authenticate(self, config: ResolvedConnectorConfig) -> _ConfluenceClient:
        """Build an authenticated Confluence client from `config`.

        `config.credential_ref` is expected to hold `"<email>:<api_token>"`
        (the same Atlassian Cloud API-token pair shape `JiraConnector`
        expects), base64-encoded here into a Basic-auth header. Calls `GET
        /wiki/rest/api/space` (limit 1) once so a misconfigured/revoked
        token or wrong `base_url` fails loudly here rather than on the first
        real fetch.
        """
        base_url = config.config.get("base_url", "").rstrip("/")
        if not base_url:
            raise RuntimeError("Confluence connector config is missing required 'base_url'")

        encoded_credentials = base64.b64encode(config.credential_ref.encode("utf-8")).decode(
            "ascii"
        )
        http = httpx.AsyncClient(
            base_url=f"{base_url}/wiki/rest/api/",
            headers={
                "Authorization": f"Basic {encoded_credentials}",
                "Accept": "application/json",
            },
            timeout=30.0,
        )
        try:
            response = await http.get("space", params={"limit": 1})
            response.raise_for_status()
        except Exception:
            await http.aclose()
            logger.warning(
                "confluence_authenticate_failed",
                connector_config_id=str(config.connector_config_id),
            )
            raise

        spaces = list(config.config.get("spaces", []))
        logger.info(
            "confluence_authenticate_succeeded",
            connector_config_id=str(config.connector_config_id),
            space_count=len(spaces),
        )
        return _ConfluenceClient(http=http, spaces=spaces, base_url=base_url)

    async def fetch_batch(
        self,
        client: _ConfluenceClient,
        *,
        since: datetime | None,
        cursor: str | None,
    ) -> FetchResult:
        """Fetch one page of raw Confluence pages from `client.spaces`.

        `cursor` is this connector's own opaque JSON envelope
        `{"space_index": int, "start": int}` -- the same "resume mid-list,
        not just mid-page" shape `JiraConnector`'s `{"project_index",
        "start_at"}` cursor uses.

        Each raw item is Confluence's content dict with `"_space_key"`/
        `"_base_url"` injected (needed by `normalize`, which only ever sees
        one self-contained `raw_item` at a time -- `_base_url` is needed
        because Confluence's own `_links.webui` is only a relative path, not
        a full URL).
        """
        space_index, start = self._decode_cursor(cursor)

        if space_index >= len(client.spaces):
            return FetchResult(items=[], next_cursor=None, has_more=False)

        space_key = client.spaces[space_index]
        cql = f'space = "{space_key}" AND type in ("page","blogpost","comment","attachment")'
        if since is not None:
            since_str = since.astimezone(timezone.utc).strftime("%Y/%m/%d %H:%M")
            cql += f' AND lastmodified >= "{since_str}"'
        cql += " ORDER BY lastmodified ASC"

        response = await client.http.get(
            "content/search",
            params={
                "cql": cql,
                "start": start,
                "limit": _SEARCH_PAGE_SIZE,
                "expand": _EXPAND,
            },
        )
        response.raise_for_status()
        payload = response.json()

        raw_results: list[dict[str, Any]] = payload.get("results", [])
        results: list[dict[str, Any]] = []
        for item in raw_results:
            item["_space_key"] = space_key
            item["_base_url"] = client.base_url
            if item.get("type") == "attachment":
                content = await self._fetch_attachment_content(client, item)
                if content is None:
                    continue  # no download link, unsupported extension, or parse failure
                item["_attachment_content"] = content
            results.append(item)

        # Length-based heuristic, not a total-count field -- CQL search
        # responses don't reliably return a `totalSize`, the same
        # "Length-based heuristic, not GitHub's Link header" reasoning
        # `GitHubConnector._list_changed_paths_page` already documents for
        # its own paginated endpoint. Deliberately `len(raw_results)`, not
        # `len(results)`: `start` is an offset into Confluence's *own*
        # result set, and the exhaustion check needs to know how many raw
        # items this page actually had -- an attachment skipped for lacking
        # a download link (or failing to parse) must not shrink either of
        # those, or the next page would re-request already-seen items.
        space_exhausted = len(raw_results) < _SEARCH_PAGE_SIZE

        if not space_exhausted:
            next_state = {"space_index": space_index, "start": start + len(raw_results)}
            has_more = True
        else:
            next_space_index = space_index + 1
            next_state = {"space_index": next_space_index, "start": 0}
            has_more = next_space_index < len(client.spaces)

        return FetchResult(
            items=results,
            next_cursor=json.dumps(next_state) if has_more else None,
            has_more=has_more,
        )

    async def _fetch_attachment_content(
        self, client: _ConfluenceClient, item: dict[str, Any]
    ) -> str | None:
        """Download and extract one attachment's text content via its own
        `_links.download` path, or `None` if there's no download link or
        `office_extraction.extract_text` can't parse it -- see module
        docstring's "skipped, not an error" note. `follow_redirects=True`
        because Atlassian's own attachment-download endpoints are
        documented to 302-redirect to the actual file.
        """
        download_path = (item.get("_links") or {}).get("download")
        if not download_path:
            return None
        download_url = f"{client.base_url}/wiki{download_path}"
        response = await client.http.get(download_url, follow_redirects=True)
        try:
            response.raise_for_status()
        except Exception:
            return None
        return extract_text(item.get("title") or "", response.content)

    def normalize(self, raw_item: Any) -> RawDocument:
        """Convert one raw Confluence content dict (with `"_space_key"`/
        `"_base_url"`/possibly `"_attachment_content"` injected by
        `fetch_batch`) into a `RawDocument`.
        """
        space_key = raw_item["_space_key"]
        base_url = raw_item["_base_url"]
        content_id = raw_item["id"]
        title = raw_item.get("title") or ""
        attachment_content = raw_item.get("_attachment_content")
        if attachment_content is not None:
            body_value = attachment_content
        else:
            body_value = ((raw_item.get("body") or {}).get("storage") or {}).get("value") or ""

        metadata: dict[str, str] = {"space": space_key, "kind": raw_item.get("type", "page")}
        version = raw_item.get("version") or {}
        if version.get("when"):
            metadata["updated"] = version["when"]
        if version.get("number") is not None:
            metadata["version"] = str(version["number"])
        author = (version.get("by") or {}).get("displayName")
        if author:
            metadata["author"] = author
        parent_id = (raw_item.get("container") or {}).get("id")
        if parent_id:
            metadata["parent_id"] = str(parent_id)

        webui_path = ((raw_item.get("_links") or {}).get("webui")) or ""
        source_url = f"{base_url}/wiki{webui_path}" if webui_path else None

        return RawDocument(
            source=self.source_name,
            external_id=f"{space_key}:{content_id}",
            content=body_value,
            title=title or None,
            source_url=source_url,
            metadata=metadata,
        )

    async def close(self, client: _ConfluenceClient) -> None:
        """Close the underlying `httpx.AsyncClient` opened by `authenticate`."""
        await client.http.aclose()

    @staticmethod
    def _decode_cursor(cursor: str | None) -> tuple[int, int]:
        """Parse this connector's opaque cursor envelope back into
        `(space_index, start)`, defaulting to the first space at `start=0`
        when `cursor` is None (a full sync's first page, or an incremental
        sync's first page for this run).
        """
        if cursor is None:
            return 0, 0
        state = json.loads(cursor)
        return int(state["space_index"]), int(state["start"])
