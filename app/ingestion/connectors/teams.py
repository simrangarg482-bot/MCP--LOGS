"""Microsoft Teams connector -- the second of Milestone 9's remaining
connectors (PROJECT_PLAN.md section 14), following `jira.py`.

Implements `app.ingestion.connectors.base.Connector` structurally (a
`Protocol` -- see that module's docstring for why no explicit inheritance is
used). Follows `SlackConnector`'s shape closely: both are chat sources, one
channel at a time, one `RawDocument` per message -- `"teams"` was in fact
already anticipated as a chat source in
`app.ingestion.processors.chunking._CHAT_SOURCES` before this connector
existed, exactly for this reason (see that module's own comment).

Uses Microsoft Graph API v1.0 directly via `httpx` (same "already a core
dependency, no source-specific SDK needed for a handful of REST calls"
reasoning as `SlackConnector`'s docstring) rather than the `msgraph-sdk`
package.

Expected `ResolvedConnectorConfig.config` shape for this source:
    {"team_id": "<team-guid>", "channels": ["<channel-id-1>", ...]}
Deliberately Graph object IDs, not display names -- the same "IDs the IT
Admin already knows, not names this connector would have to resolve" choice
`SlackConnector` made for channel IDs and `JiraConnector` made for project
keys.

`config.credential_ref` is expected to be a already-issued OAuth2 access
token (Graph API requires bearer-token auth, obtained via the organization's
own app registration + client-credentials flow at connector-setup time, not
something this connector performs itself) -- same flagged "literal value
until `shared/security` exists" placeholder every other connector's
docstring already carries.

Known limitation, flagged rather than silently built around: Graph's
"list channel messages" endpoint (`GET
/teams/{id}/channels/{id}/messages`) has no server-side `since`/`$filter`
support for incremental sync the way Slack's `oldest` param or Jira's JQL
`updated >=` clause do -- Graph's *delta query* API
(`/messages/delta`) is the documented mechanism for that, and would be the
correct long-term answer, but is a meaningfully different pagination model
(opaque delta tokens persisted *across* syncs, not just across pages of one
sync) than this first pass implements. For now, `since` is applied as a
client-side filter per page after fetching -- correct output, but an
incremental sync still walks a channel's full message history under the
hood, the same tradeoff `GitHubConnector`'s "Known limitations" note already
accepts elsewhere in this codebase for a first-pass connector.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from app.ingestion.schemas import FetchResult, RawDocument, ResolvedConnectorConfig
from app.shared.config.logging import get_logger

logger = get_logger(__name__)

_GRAPH_API_BASE_URL = "https://graph.microsoft.com/v1.0/"
_MESSAGES_PAGE_SIZE = 50


@dataclass
class _TeamsClient:
    """What `TeamsConnector.authenticate` returns as `AuthenticatedClient`.

    Bundles the authenticated HTTP client with the fixed `team_id` and
    channel-ID list from `ResolvedConnectorConfig.config` -- `fetch_batch`
    only receives this object back, not the original config, same reasoning
    as `_SlackClient`/`_GitHubClient`/`_JiraClient`.
    """

    http: httpx.AsyncClient
    team_id: str
    channels: list[str]


class TeamsConnector:
    """Fetches message history from a fixed set of channels in one Microsoft
    Team. Only reads message history -- posting, reactions, and file
    attachments are out of scope, same restriction `SlackConnector` declares
    for the same reasons (PROJECT_PLAN.md section 4.1: a connector
    normalizes, nothing more).
    """

    source_name = "teams"
    # Microsoft Graph's throttling is resource-unit-based per app/tenant, not
    # a single published flat requests/second ceiling (unlike GitHub's
    # 5,000/hour) -- same "no single authoritative number" situation
    # `JiraConnector` documents for its own rate limit. 1.0 req/s is a
    # conservative steady-state budget for the channel-messages endpoint
    # specifically, matching section 4.5's "a conservative constant is fine"
    # allowance.
    requests_per_second = 1.0

    async def authenticate(self, config: ResolvedConnectorConfig) -> _TeamsClient:
        """Build an authenticated Graph client from `config`.

        `config.credential_ref` is treated as a literal, already-issued
        bearer access token -- see module docstring. Calls `GET /me` once so
        an invalid/expired token fails loudly here rather than on the first
        real fetch, matching `SlackConnector.authenticate`'s `auth.test`
        precedent (Graph has no dedicated "verify this token" endpoint, but
        `/me` is a cheap, always-available call for any valid token).
        """
        team_id = config.config.get("team_id", "")
        if not team_id:
            raise RuntimeError("Teams connector config is missing required 'team_id'")

        http = httpx.AsyncClient(
            base_url=_GRAPH_API_BASE_URL,
            headers={"Authorization": f"Bearer {config.credential_ref}"},
            timeout=30.0,
        )
        try:
            response = await http.get("me")
            response.raise_for_status()
        except Exception:
            await http.aclose()
            logger.warning(
                "teams_authenticate_failed",
                connector_config_id=str(config.connector_config_id),
            )
            raise

        channels = list(config.config.get("channels", []))
        logger.info(
            "teams_authenticate_succeeded",
            connector_config_id=str(config.connector_config_id),
            team_id=team_id,
            channel_count=len(channels),
        )
        return _TeamsClient(http=http, team_id=team_id, channels=channels)

    async def fetch_batch(
        self,
        client: _TeamsClient,
        *,
        since: datetime | None,
        cursor: str | None,
    ) -> FetchResult:
        """Fetch one page of raw Teams messages from `client.channels`.

        `cursor` is this connector's own opaque JSON envelope
        `{"channel_index": int, "next_link": str | None}` -- `next_link` is
        Graph's own full `@odata.nextLink` URL (an opaque, absolute URL
        Graph expects to be requested as-is, unlike Slack's/Jira's own
        smaller native cursor tokens), the same "resume mid-list, not just
        mid-page" shape every other connector's cursor uses.

        Each raw item is Graph's message dict with `"_channel_id"` injected
        (needed by `normalize`, which only ever sees one self-contained
        `raw_item` at a time; Graph's own message payload has no channel
        field, same gap `SlackConnector.fetch_batch` fills for Slack).
        """
        channel_index, next_link = self._decode_cursor(cursor)

        if channel_index >= len(client.channels):
            return FetchResult(items=[], next_cursor=None, has_more=False)

        channel_id = client.channels[channel_index]

        if next_link is not None:
            response = await client.http.get(next_link)
        else:
            response = await client.http.get(
                f"teams/{client.team_id}/channels/{channel_id}/messages",
                params={"$top": _MESSAGES_PAGE_SIZE},
            )
        response.raise_for_status()
        payload = response.json()

        messages: list[dict[str, Any]] = payload.get("value", [])
        if since is not None:
            messages = [m for m in messages if self._is_recent_enough(m, since)]
        for message in messages:
            message["_channel_id"] = channel_id

        graph_next_link = payload.get("@odata.nextLink")

        if graph_next_link:
            next_state = {"channel_index": channel_index, "next_link": graph_next_link}
            has_more = True
        else:
            next_channel_index = channel_index + 1
            next_state = {"channel_index": next_channel_index, "next_link": None}
            has_more = next_channel_index < len(client.channels)

        return FetchResult(
            items=messages,
            next_cursor=json.dumps(next_state) if has_more else None,
            has_more=has_more,
        )

    def normalize(self, raw_item: Any) -> RawDocument:
        """Convert one raw Teams message dict (with `_channel_id` injected
        by `fetch_batch`) into a `RawDocument`.
        """
        channel_id = raw_item["_channel_id"]
        message_id = raw_item["id"]
        body = raw_item.get("body") or {}
        content = body.get("content", "")

        metadata: dict[str, str] = {"channel_id": channel_id}
        author = ((raw_item.get("from") or {}).get("user") or {}).get("displayName")
        if author:
            metadata["author"] = author
        if raw_item.get("createdDateTime"):
            metadata["created"] = raw_item["createdDateTime"]
        reply_to_id = raw_item.get("replyToId")
        if reply_to_id:
            metadata["reply_to_id"] = reply_to_id

        return RawDocument(
            source=self.source_name,
            external_id=f"{channel_id}:{message_id}",
            content=content,
            title=None,
            source_url=raw_item.get("webUrl"),
            metadata=metadata,
        )

    async def close(self, client: _TeamsClient) -> None:
        """Close the underlying `httpx.AsyncClient` opened by `authenticate`."""
        await client.http.aclose()

    @staticmethod
    def _is_recent_enough(message: dict[str, Any], since: datetime) -> bool:
        """Client-side `since` filter -- see module docstring's "Known
        limitations" note on why this isn't a server-side query parameter.
        Messages with an unparseable/missing `createdDateTime` are kept
        (same "absence of a timestamp isn't evidence of staleness" reasoning
        `agents.service._passes_recency_filter` already uses elsewhere in
        this codebase), not silently dropped.
        """
        raw_timestamp = message.get("createdDateTime")
        if not raw_timestamp:
            return True
        try:
            created_at = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except ValueError:
            return True
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return created_at >= since

    @staticmethod
    def _decode_cursor(cursor: str | None) -> tuple[int, str | None]:
        """Parse this connector's opaque cursor envelope back into
        `(channel_index, next_link)`, defaulting to the first channel with
        no Graph-native `next_link` when `cursor` is None (a full sync's
        first page, or an incremental sync's first page for this run).
        """
        if cursor is None:
            return 0, None
        state = json.loads(cursor)
        return int(state["channel_index"]), state.get("next_link")
