"""Slack connector -- one of the first two sources for Milestone 4
(PROJECT_PLAN.md section 4: "recommend starting with Slack and GitHub").

Implements `app.ingestion.connectors.base.Connector` structurally (a
`Protocol`, so no explicit inheritance is needed or wanted -- see that
module's docstring for why). Uses `httpx` directly against Slack's Web API
rather than the `slack-sdk` package: `httpx` is already a core dependency
(added for core/auth's OIDC calls), and pulling in a second HTTP client
purely for one connector would be an unnecessary dependency for what is a
handful of plain REST calls (Slack's Web API needs no SDK-specific behavior
this connector relies on).

Expected `ResolvedConnectorConfig.config` shape for this source:
    {"channels": ["C0123456", "C0234567", ...]}   # Slack channel IDs

Deliberately channel IDs, not names: resolving a human channel name to an ID
requires either `conversations.list` (paginated itself, and a bot must be a
member of a private channel to see it) or a separate lookup step the IT
Admin onboarding flow (core/tenancy) would need to run once during
`register_connector` -- out of scope for this connector, which only fetches
from channels it is already told about.

Note on the `Connector` protocol's shape: `fetch_batch(client, since,
cursor)` does not take the resolved config -- only whatever `authenticate`
returned as `client`. Since `AuthenticatedClient` is deliberately `Any` (see
base.py's docstring: "there is no meaningful common shape across sources"),
this connector's `authenticate` returns a small `_SlackClient` bundling the
real `httpx.AsyncClient` together with the channel list `fetch_batch` needs,
rather than the bare `httpx.AsyncClient` alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from app.ingestion.schemas import FetchResult, RawDocument, ResolvedConnectorConfig
from app.shared.config.logging import get_logger

logger = get_logger(__name__)

_SLACK_API_BASE_URL = "https://slack.com/api/"
_HISTORY_PAGE_SIZE = 200  # Slack's max per-page limit for conversations.history


@dataclass
class _SlackClient:
    """What `SlackConnector.authenticate` returns as `AuthenticatedClient`.

    Bundles the authenticated HTTP client with the fixed channel list from
    `ResolvedConnectorConfig.config["channels"]`, since `fetch_batch` itself
    only receives this object back, not the original config.
    """

    http: httpx.AsyncClient
    channels: list[str]


class SlackConnector:
    """Fetches message history from a fixed set of Slack channels.

    Only reads message history -- posting, reactions, and files are out of
    scope for this connector; a message's own text is the unit of content
    ingested (PROJECT_PLAN.md section 4.1: a connector normalizes, nothing
    more).
    """

    source_name = "slack"
    # Slack's Tier 3 method rate limit (conversations.history) is roughly
    # 50+ requests/minute per workspace; ~0.5 req/s is a conservative
    # steady-state budget that stays well clear of a 429, matching section
    # 4.5's "declare a conservative constant" allowance.
    requests_per_second = 0.5

    async def authenticate(self, config: ResolvedConnectorConfig) -> _SlackClient:
        """Build an authenticated Slack client from `config`.

        `config.credential_ref` is treated as the literal bot token
        (`xoxb-...`) for now -- see `ResolvedConnectorConfig`'s docstring on
        why this is a flagged placeholder, not a real secret-store
        resolution. Calls `auth.test` once so a misconfigured/revoked token
        fails loudly here rather than silently on the first `fetch_batch`.
        """
        http = httpx.AsyncClient(
            base_url=_SLACK_API_BASE_URL,
            headers={"Authorization": f"Bearer {config.credential_ref}"},
            timeout=30.0,
        )
        try:
            response = await http.post("auth.test")
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok", False):
                logger.warning(
                    "slack_authenticate_failed",
                    connector_config_id=str(config.connector_config_id),
                    slack_error=payload.get("error"),
                )
                raise RuntimeError(
                    f"Slack auth.test failed: {payload.get('error', 'unknown_error')}"
                )
        except Exception:
            await http.aclose()
            raise

        logger.info(
            "slack_authenticate_succeeded",
            connector_config_id=str(config.connector_config_id),
            team=payload.get("team"),
        )
        channels = list(config.config.get("channels", []))
        return _SlackClient(http=http, channels=channels)

    async def fetch_batch(
        self,
        client: _SlackClient,
        *,
        since: datetime | None,
        cursor: str | None,
    ) -> FetchResult:
        """Fetch one page of raw Slack messages from `client.channels`.

        A `connector_config` can list multiple channels, but Slack paginates
        one channel at a time -- `cursor` here is this connector's own
        opaque JSON envelope `{"channel_index": int, "slack_cursor": str |
        None}`, not Slack's `next_cursor` directly, so pagination can resume
        mid-channel-list rather than only mid-channel.

        Each raw item is Slack's message dict with `"_channel_id"` injected
        (Slack's own message payload has no channel field -- it's implied by
        which `conversations.history` call returned it -- but `normalize`
        needs it, and `normalize` only ever sees one self-contained
        `raw_item` at a time).
        """
        channel_index, slack_cursor = self._decode_cursor(cursor)

        if channel_index >= len(client.channels):
            return FetchResult(items=[], next_cursor=None, has_more=False)

        channel_id = client.channels[channel_index]
        params: dict[str, str | int] = {"channel": channel_id, "limit": _HISTORY_PAGE_SIZE}
        if slack_cursor:
            params["cursor"] = slack_cursor
        if since is not None:
            params["oldest"] = str(since.timestamp())

        response = await client.http.get("conversations.history", params=params)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok", False):
            logger.warning(
                "slack_fetch_batch_failed",
                channel_id=channel_id,
                slack_error=payload.get("error"),
            )
            raise RuntimeError(
                f"Slack conversations.history failed: {payload.get('error', 'unknown_error')}"
            )

        messages: list[dict[str, Any]] = payload.get("messages", [])
        for message in messages:
            message["_channel_id"] = channel_id

        slack_has_more = bool(payload.get("has_more", False))
        slack_next_cursor = payload.get("response_metadata", {}).get("next_cursor") or None

        if slack_has_more and slack_next_cursor:
            # Same channel, next Slack-native page.
            next_state = {"channel_index": channel_index, "slack_cursor": slack_next_cursor}
            has_more = True
        else:
            # This channel is exhausted; advance to the next one, if any.
            next_channel_index = channel_index + 1
            next_state = {"channel_index": next_channel_index, "slack_cursor": None}
            has_more = next_channel_index < len(client.channels)

        return FetchResult(
            items=messages,
            next_cursor=json.dumps(next_state) if has_more else None,
            has_more=has_more,
        )

    def normalize(self, raw_item: Any) -> RawDocument:
        """Convert one raw Slack message dict (with `_channel_id` injected
        by `fetch_batch`) into a `RawDocument`.
        """
        channel_id = raw_item["_channel_id"]
        ts = raw_item["ts"]
        metadata = {"channel_id": channel_id}
        if raw_item.get("user"):
            metadata["user"] = raw_item["user"]
        if raw_item.get("thread_ts") and raw_item["thread_ts"] != ts:
            metadata["thread_ts"] = raw_item["thread_ts"]

        return RawDocument(
            source=self.source_name,
            external_id=f"{channel_id}:{ts}",
            content=raw_item.get("text", ""),
            title=None,
            # Slack permalinks require a separate chat.getPermalink call per
            # message (an extra API call per item, against a tighter rate
            # limit than conversations.history) -- deferred, flagged rather
            # than silently built, since it would meaningfully change this
            # connector's request budget.
            source_url=None,
            metadata=metadata,
        )

    async def close(self, client: _SlackClient) -> None:
        """Close the underlying `httpx.AsyncClient` opened by `authenticate`."""
        await client.http.aclose()

    @staticmethod
    def _decode_cursor(cursor: str | None) -> tuple[int, str | None]:
        """Parse this connector's opaque cursor envelope back into
        `(channel_index, slack_cursor)`, defaulting to the first channel
        with no Slack-native cursor when `cursor` is None (a full sync's
        first page, or an incremental sync's first page for this run).
        """
        if cursor is None:
            return 0, None
        state = json.loads(cursor)
        return int(state["channel_index"]), state.get("slack_cursor")
