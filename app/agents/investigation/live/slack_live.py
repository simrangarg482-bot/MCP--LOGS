"""Live Slack evidence source for the Investigation Agent -- fetches recent
messages directly from configured channels, filtered by a naive keyword
match against `query`, rather than from whatever
`ingestion.connectors.slack.SlackConnector` has already indexed into the
"conversations" collection.

Deliberately does NOT use Slack's `search.messages` API, even though that
would give real relevance-ranked search the way `GitHubLiveSource` uses
GitHub's Search API: `search.messages` requires a *user* token
(`xoxp-...`, `search:read` scope) -- a Slack bot token (`xoxb-...`) cannot
call it at all; a bot "cannot search the workspace," per Slack's own API
documentation. This system's `ConnectorConfig.credential_ref` for a Slack
connector is the same bot token `SlackConnector.authenticate` already
treats as a literal credential placeholder (see that module's docstring) --
there is no second, user-scoped credential modeled anywhere in this system
today, and adding one is out of scope for this feature. Rather than
silently pretending to do real search, or requiring a whole new credential
type this feature doesn't otherwise need, this class instead re-fetches
recent history via `conversations.history` (the exact same endpoint and bot
token scope the ingestion connector already relies on) across the
configured channels, and applies a simple case-insensitive keyword filter
client-side. This is a real, flagged fidelity trade-off -- naive keyword
matching, not full-text search -- documented here rather than overclaimed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.tenancy.schemas import ConnectorConfig
from app.shared.config.logging import get_logger
from app.shared.schemas import EvidenceItem

logger = get_logger(__name__)

_SLACK_API_BASE_URL = "https://slack.com/api/"
_MAX_CHANNELS_PER_CALL = 5  # bounds latency/rate-limit usage per lookup
_HISTORY_PAGE_SIZE = 100
_SUMMARY_MAX_CHARS = 300
_MIN_KEYWORD_LENGTH = 3  # skip short/common words ("is", "to", ...) as filter terms


class SlackLiveSource:
    """Live, keyword-filtered recent Slack messages -- see module
    docstring.
    """

    source_name = "slack"
    # Matches `SlackConnector.requests_per_second` -- same endpoint, same
    # workspace-level Tier 3 rate budget.
    requests_per_second = 0.5

    async def fetch_live_evidence(
        self,
        *,
        connector_config: ConnectorConfig,
        query: str,
        since: datetime,
        limit: int,
    ) -> list[EvidenceItem]:
        channels: list[str] = list(connector_config.config.get("channels", []))[
            :_MAX_CHANNELS_PER_CALL
        ]
        if not channels:
            return []

        keywords = [word.lower() for word in query.split() if len(word) >= _MIN_KEYWORD_LENGTH]
        headers = {"Authorization": f"Bearer {connector_config.credential_ref}"}

        evidence: list[EvidenceItem] = []
        async with httpx.AsyncClient(
            base_url=_SLACK_API_BASE_URL, headers=headers, timeout=15.0
        ) as http:
            for channel_id in channels:
                evidence.extend(
                    await self._fetch_channel_matches(http, channel_id, since, keywords)
                )

        evidence.sort(key=lambda item: item.source_timestamp or since, reverse=True)
        return evidence[:limit]

    async def _fetch_channel_matches(
        self,
        http: httpx.AsyncClient,
        channel_id: str,
        since: datetime,
        keywords: list[str],
    ) -> list[EvidenceItem]:
        try:
            response = await http.get(
                "conversations.history",
                params={
                    "channel": channel_id,
                    "oldest": str(since.timestamp()),
                    "limit": _HISTORY_PAGE_SIZE,
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok", False):
                logger.warning(
                    "slack_live_fetch_failed",
                    channel_id=channel_id,
                    slack_error=payload.get("error"),
                )
                return []
        except Exception as exc:
            logger.warning("slack_live_fetch_failed", channel_id=channel_id, error=str(exc))
            return []

        messages: list[dict[str, Any]] = payload.get("messages", [])
        matches = [message for message in messages if self._matches(message.get("text", ""), keywords)]

        evidence: list[EvidenceItem] = []
        for message in matches:
            ts = message.get("ts", "")
            metadata = {"channel_id": channel_id, "retrieval_mode": "live"}
            if message.get("user"):
                metadata["user"] = message["user"]
            evidence.append(
                EvidenceItem(
                    source="slack",
                    reference=f"channel:{channel_id}:ts:{ts}",
                    summary=self._truncate(message.get("text", "")),
                    retrieved_at=datetime.now(timezone.utc),
                    source_timestamp=self._parse_ts(ts),
                    metadata=metadata,
                )
            )
        return evidence

    @staticmethod
    def _matches(text: str, keywords: list[str]) -> bool:
        if not keywords:
            return True
        lowered = text.lower()
        return any(keyword in lowered for keyword in keywords)

    @staticmethod
    def _truncate(text: str) -> str:
        if len(text) > _SUMMARY_MAX_CHARS:
            return text[:_SUMMARY_MAX_CHARS] + "..."
        return text

    @staticmethod
    def _parse_ts(value: str) -> datetime | None:
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, TypeError):
            return None
