"""SharePoint connector -- the fifth and last of Milestone 9's document/chat
connectors (PROJECT_PLAN.md section 14), following `teams.py`: both use
Microsoft Graph API with an already-issued bearer access token.

Implements `app.ingestion.connectors.base.Connector` structurally (a
`Protocol` -- see that module's docstring for why no explicit inheritance is
used).

Expected `ResolvedConnectorConfig.config` shape for this source:
    {"site_ids": ["<graph-site-id-1>", "<graph-site-id-2>", ...]}
Graph *site IDs* (not site URLs/names) -- the same "IDs the IT Admin already
knows" choice every other multi-container connector built this milestone
makes (`JiraConnector`'s projects, `AzureDevOpsConnector`'s projects,
`ConfluenceConnector`'s spaces).

Uses Graph's drive **delta** endpoint (`/sites/{id}/drive/root/delta`) to
list a site's default document library recursively in one flat, paginated
walk -- unlike a plain `/children` listing, which only returns one folder
level at a time and would need this connector to walk the folder tree
itself. `supports_resume_token = True`: each site's `@odata.deltaLink` is
persisted across separate sync runs (`_execute_ingestion_job` writes
`FetchResult.resume_token` into `connector_configs.config["_resume_token"]`
on success), so an incremental sync resumes each site directly from its
own saved deltaLink instead of re-walking that site's whole tree from
scratch every time -- see `_SharePointClient.resume_state` and
`fetch_batch`'s own docstring for the mechanics. `since` is still applied
as a client-side filter on `lastModifiedDateTime` on top of this (Graph's
delta response itself carries no `$filter`-style query support), the same
fallback `TeamsConnector` uses for its own comparable gap.

Plain-text files, plus Word/PDF/Excel documents, have their content
fetched and ingested; folders and every other file type (images,
PowerPoint, ...) are listed by the delta walk but skipped, not erroring --
the same "skipped, not an error" treatment `GitHubConnector._fetch_file_
content` already gives binary/undecodable files. `.pdf`/`.docx`/`.xlsx`
extraction itself lives in `app.ingestion.office_extraction` (`pypdf`/
`python-docx`/`openpyxl` respectively), a shared module rather than
connector-local code once `ConfluenceConnector`'s own attachment-content
gap needed the exact same capability -- see that module's docstring for
the extraction details and its own "skipped, not an error" contract on
parse failures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from app.ingestion.office_extraction import extract_text
from app.ingestion.schemas import FetchResult, RawDocument, ResolvedConnectorConfig
from app.shared.config.logging import get_logger

logger = get_logger(__name__)

_GRAPH_API_BASE_URL = "https://graph.microsoft.com/v1.0/"
_PLAIN_TEXT_EXTENSIONS = {".txt", ".md", ".markdown"}
# Office/PDF extensions `office_extraction.extract_text` knows how to parse
# -- kept in sync with that module's own `_EXTRACTORS`, duplicated here only
# as an extension *name* set (not the parsing logic itself) so
# `_fetch_text_content` can decide "is this even worth downloading" before
# making the network call.
_OFFICE_EXTENSIONS = {".pdf", ".docx", ".xlsx"}


@dataclass
class _SharePointClient:
    """What `SharePointConnector.authenticate` returns as
    `AuthenticatedClient`. Bundles the authenticated HTTP client with the
    fixed site-ID list -- `fetch_batch` only receives this object back, not
    the original config, same reasoning as `_TeamsClient`.

    `resume_state` (`{site_id: deltaLink}`) accumulates across every
    `fetch_batch` call within one sync -- seeded once from the incoming
    `resume_token` on the first call, updated each time a site's own delta
    walk completes. It has to live here, on the one object every call this
    sync shares, rather than being recomputed per call, because a later
    call has no other way to know what an *earlier* call in the same sync
    already learned about a different site.
    """

    http: httpx.AsyncClient
    site_ids: list[str]
    resume_state: dict[str, str] = field(default_factory=dict)


class SharePointConnector:
    """Fetches plain-text and Office/PDF files from a fixed set of
    SharePoint sites' default document libraries -- see module docstring
    for the exact supported-format list.
    """

    source_name = "sharepoint"
    # Same "no single published flat ceiling" situation `TeamsConnector`
    # documents for Graph API in general. 1.0 req/s is a conservative
    # steady-state budget for the delta-listing + per-file content-download
    # calls this connector makes.
    requests_per_second = 1.0
    supports_resume_token = True

    async def authenticate(self, config: ResolvedConnectorConfig) -> _SharePointClient:
        """Build an authenticated Graph client from `config`.

        `config.credential_ref` is an already-issued bearer access token,
        already decrypted by `app.ingestion.service` via `shared/security`
        before this connector ever sees it -- see `base.Connector.
        authenticate`'s docstring, same as `TeamsConnector.authenticate`.
        Probes `GET /sites/{site_id}` against
        the first configured site once, so an invalid/expired token, or an
        unreachable site id, fails loudly here rather than on the first real
        fetch.

        NOT `GET /me`, which this connector used originally: `/me` resolves
        the *signed-in user* and is only valid for delegated authentication.
        An application-permission (client-credentials) token has no user
        context, so Graph rejects `/me` with `NoPermissionsInAccessToken` --
        and client-credentials is exactly what this module's own docstring
        tells operators to configure. See `TeamsConnector.authenticate`,
        which had the identical defect and is fixed the same way.

        `site_ids` is now read *before* the probe (it was read after), since
        the probe needs a site to call. A config with no `site_ids` at all
        raises rather than authenticating: there is nothing to probe, and
        nothing this connector could ever fetch -- the same "missing
        required config fails loudly at authenticate()" treatment
        `TeamsConnector` already gives a missing `team_id`.
        """
        site_ids = list(config.config.get("site_ids", []))
        if not site_ids:
            raise RuntimeError("SharePoint connector config is missing required 'site_ids'")

        http = httpx.AsyncClient(
            base_url=_GRAPH_API_BASE_URL,
            headers={"Authorization": f"Bearer {config.credential_ref}"},
            timeout=30.0,
        )
        try:
            response = await http.get(f"sites/{site_ids[0]}")
            response.raise_for_status()
        except Exception:
            await http.aclose()
            logger.warning(
                "sharepoint_authenticate_failed",
                connector_config_id=str(config.connector_config_id),
            )
            raise

        logger.info(
            "sharepoint_authenticate_succeeded",
            connector_config_id=str(config.connector_config_id),
            site_count=len(site_ids),
        )
        return _SharePointClient(http=http, site_ids=site_ids)

    async def fetch_batch(
        self,
        client: _SharePointClient,
        *,
        since: datetime | None,
        cursor: str | None,
        resume_token: str | None = None,
    ) -> FetchResult:
        """Fetch one page of raw SharePoint drive items from
        `client.site_ids`.

        `cursor` is this connector's own opaque JSON envelope
        `{"site_index": int, "next_link": str | None}` -- `next_link` is
        Graph's own full `@odata.nextLink` URL, the same "resume mid-list
        via Graph's own opaque absolute URL" shape `TeamsConnector`'s cursor
        already uses.

        `resume_token` (only ever non-`None` because `supports_resume_token
        = True`) is this sync's persisted `{site_id: deltaLink}` map from
        the *previous* sync -- decoded once, into `client.resume_state`, the
        first time this method runs this sync. When a site's walk is about
        to start fresh (`next_link is None` for it) and `client.resume_state`
        already has a saved deltaLink for that site, that deltaLink is
        requested directly instead of the bare `/delta` root -- Graph's own
        documented mechanism for "only what changed since last time,"
        closing the "every sync re-walks everything from scratch" gap this
        module's docstring used to carry. `_execute_ingestion_job` (the only
        caller) persists whatever `FetchResult.resume_token` this returns on
        the sync's last page as the next sync's input.

        Only file-type items with a supported text extension and a fetched
        content body end up in the returned page -- folders, unsupported
        file types, and content-download failures are filtered out here,
        inside `fetch_batch`, not in `normalize` (which is sync-only and
        cannot itself perform the content-download network call -- the same
        "do the I/O in `fetch_batch`, attach the result onto the raw item"
        shape `GitHubConnector._list_file_items_page` already uses for its
        own per-file content fetch).
        """
        if resume_token is not None and not client.resume_state:
            try:
                client.resume_state = json.loads(resume_token)
            except (json.JSONDecodeError, TypeError):
                client.resume_state = {}

        site_index, next_link = self._decode_cursor(cursor)

        if site_index >= len(client.site_ids):
            return FetchResult(items=[], next_cursor=None, has_more=False)

        site_id = client.site_ids[site_index]

        if next_link is not None:
            response = await client.http.get(next_link)
        else:
            saved_delta_link = client.resume_state.get(site_id)
            start_url = saved_delta_link or f"sites/{site_id}/drive/root/delta"
            response = await client.http.get(start_url)
        response.raise_for_status()
        payload = response.json()

        raw_entries: list[dict[str, Any]] = payload.get("value", [])
        items: list[dict[str, Any]] = []
        for entry in raw_entries:
            if "file" not in entry:
                continue  # a folder, or a deleted-item tombstone -- not ingestible content
            if since is not None and not self._is_recent_enough(entry, since):
                continue
            content = await self._fetch_text_content(client.http, entry)
            if content is None:
                continue  # unsupported extension, or download/decode failure -- skipped
            entry["_site_id"] = site_id
            entry["_content"] = content
            items.append(entry)

        graph_next_link = payload.get("@odata.nextLink")
        resume_token_out: str | None = None

        if graph_next_link:
            next_state = {"site_index": site_index, "next_link": graph_next_link}
            has_more = True
        else:
            # No `@odata.nextLink` means Graph returned `@odata.deltaLink`
            # instead -- this site's delta walk is complete for this sync.
            graph_delta_link = payload.get("@odata.deltaLink")
            if graph_delta_link:
                client.resume_state[site_id] = graph_delta_link
                resume_token_out = json.dumps(client.resume_state)
            next_site_index = site_index + 1
            next_state = {"site_index": next_site_index, "next_link": None}
            has_more = next_site_index < len(client.site_ids)

        return FetchResult(
            items=items,
            next_cursor=json.dumps(next_state) if has_more else None,
            has_more=has_more,
            resume_token=resume_token_out,
        )

    async def _fetch_text_content(
        self, http: httpx.AsyncClient, entry: dict[str, Any]
    ) -> str | None:
        """Download and extract one file's text content, or `None` if it's
        not a supported extension or fails to decode/parse -- see module
        docstring's supported-format list and "skipped, not an error" note.
        """
        name = entry.get("name", "")
        extension = next(
            (ext for ext in (*_PLAIN_TEXT_EXTENSIONS, *_OFFICE_EXTENSIONS) if name.endswith(ext)),
            None,
        )
        if extension is None:
            return None
        download_url = entry.get("@microsoft.graph.downloadUrl")
        if not download_url:
            return None
        response = await http.get(download_url)
        try:
            response.raise_for_status()
        except Exception:
            return None

        if extension in _PLAIN_TEXT_EXTENSIONS:
            try:
                return response.text
            except Exception:
                # Any decode failure is treated the same as "not ingestible
                # text", not a reason to fail the whole batch -- same
                # "skipped, not an error" treatment `GitHubConnector.
                # _fetch_file_content` gives its own undecodable files.
                return None

        # `office_extraction.extract_text` already swallows parse failures
        # and returns None -- no separate try/except needed here.
        return extract_text(name, response.content)

    @staticmethod
    def _is_recent_enough(entry: dict[str, Any], since: datetime) -> bool:
        """Client-side `since` filter -- see module docstring's "Known
        limitations" note. Entries with an unparseable/missing
        `lastModifiedDateTime` are kept, not silently dropped, the same
        "absence of a timestamp isn't evidence of staleness" reasoning
        `TeamsConnector._is_recent_enough` already uses.
        """
        raw_timestamp = entry.get("lastModifiedDateTime")
        if not raw_timestamp:
            return True
        try:
            modified_at = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except ValueError:
            return True
        if modified_at.tzinfo is None:
            modified_at = modified_at.replace(tzinfo=timezone.utc)
        return modified_at >= since

    def normalize(self, raw_item: Any) -> RawDocument:
        """Convert one raw SharePoint drive item dict (with `"_site_id"`/
        `"_content"` injected by `fetch_batch`) into a `RawDocument`.
        """
        site_id = raw_item["_site_id"]
        item_id = raw_item["id"]
        name = raw_item.get("name", item_id)
        content = raw_item["_content"]

        metadata: dict[str, str] = {"site_id": site_id}
        if raw_item.get("lastModifiedDateTime"):
            metadata["updated"] = raw_item["lastModifiedDateTime"]
        parent_path = (raw_item.get("parentReference") or {}).get("path")
        if parent_path:
            metadata["folder_path"] = parent_path

        return RawDocument(
            source=self.source_name,
            external_id=f"{site_id}:{item_id}",
            content=content,
            title=name,
            source_url=raw_item.get("webUrl"),
            metadata=metadata,
        )

    async def close(self, client: _SharePointClient) -> None:
        """Close the underlying `httpx.AsyncClient` opened by `authenticate`."""
        await client.http.aclose()

    @staticmethod
    def _decode_cursor(cursor: str | None) -> tuple[int, str | None]:
        """Parse this connector's opaque cursor envelope back into
        `(site_index, next_link)`, defaulting to the first site with no
        Graph-native `next_link` when `cursor` is None (a full sync's first
        page, or an incremental sync's first page for this run).
        """
        if cursor is None:
            return 0, None
        state = json.loads(cursor)
        return int(state["site_index"]), state.get("next_link")
