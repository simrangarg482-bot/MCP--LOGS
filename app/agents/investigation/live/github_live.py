"""Live GitHub evidence source for the Investigation Agent -- fetches
commits/PRs/issues *directly from GitHub's REST Search API*, scoped to
whichever repos are already registered in a `ConnectorConfig`, rather than
from whatever `ingestion.connectors.github.GitHubConnector` has already
indexed into the "code"/"documentation" collections.

Deliberately separate code from that connector (not a subclass, not a
shared base class): the two have almost nothing in common beyond "call
GitHub's API with a bearer token." The ingestion connector's job is an
exhaustive, resumable, paginated walk of a repo's entire file/commit/PR/
issue history, destined for the chunk/embed/store pipeline
(`ingestion.processors.pipeline`) -- see that connector's own module
docstring's phase-cursor design. This class's job is "give me a handful of
results relevant to *this* incident, right now": one or two requests per
repo, no cursor, no persistence, tightly capped by `limit` and a lookback
window. Sharing code across the two would mean threading a "live vs.
full-sync" mode through most of that connector's methods for very little
actual reuse -- the pagination/cursor machinery, most of its real
complexity, simply does not apply here.

Uses GitHub's Search API (`/search/issues`, `/search/commits`), not the
plain list endpoints (`/commits`, `/issues`, `/pulls`) the ingestion
connector uses: only Search supports a free-text relevance query (`q=...`),
which is the whole point of a *targeted* live lookup ("related to the
incident", not just "most recent"). Trade-off: GitHub's Search API has a
much stricter rate limit than the general REST API -- 30 requests/minute
for an authenticated caller, vs. 5,000/hour (see
https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)
-- so `requests_per_second` here is deliberately conservative and the
number of repos searched per call is capped (`_MAX_REPOS_PER_CALL`) rather
than walking every registered repo unconditionally.

`/search/issues` returns both issues and pull requests (a PR is a
specialization of an issue in GitHub's data model -- the same fact
`ingestion.connectors.github._list_issues_page` already documents),
distinguished here by the presence of a `"pull_request"` key, same
convention as that module. Because this is a single Search API response
(not the list-then-per-item-detail-fetch sequence the ingestion connector
uses for PRs), fields only available from a per-PR detail call
(`merged_at`, changed files, reviews) are not available here and are simply
omitted -- a live result is a fast, partial glance, not a full-fidelity
indexed record. This is a documented trade-off, not something silently
worked around.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.tenancy.schemas import ConnectorConfig
from app.shared.config.logging import get_logger
from app.shared.schemas import EvidenceItem

logger = get_logger(__name__)

_GITHUB_API_BASE_URL = "https://api.github.com/"
_GITHUB_API_VERSION = "2022-11-28"
_MAX_REPOS_PER_CALL = 5  # bounds latency and Search API rate-limit usage per lookup
_PER_ENDPOINT_RESULTS = 10  # per repo, per endpoint (issues+PRs, commits)
_SUMMARY_MAX_CHARS = 300


class GitHubLiveSource:
    """Live, query-relevant commits/PRs/issues from GitHub's Search API --
    see module docstring.
    """

    source_name = "github"
    # GitHub's Search API allows 30 requests/minute for an authenticated
    # caller (far stricter than the general REST API's 5,000/hour -- see
    # module docstring). 0.4 req/s stays comfortably under that even if a
    # few investigations run concurrently against the same org's token.
    requests_per_second = 0.4

    async def fetch_live_evidence(
        self,
        *,
        connector_config: ConnectorConfig,
        query: str,
        since: datetime,
        limit: int,
    ) -> list[EvidenceItem]:
        repos: list[dict[str, str]] = list(connector_config.config.get("repos", []))[
            :_MAX_REPOS_PER_CALL
        ]
        if not repos:
            return []

        since_date = since.astimezone(UTC).date().isoformat()
        headers = {
            "Authorization": f"Bearer {connector_config.credential_ref}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        }

        evidence: list[EvidenceItem] = []
        async with httpx.AsyncClient(
            base_url=_GITHUB_API_BASE_URL, headers=headers, timeout=15.0
        ) as http:
            for repo_entry in repos:
                repo = repo_entry.get("repo")
                if not repo:
                    continue
                evidence.extend(
                    await self._search_issues_and_pulls(http, repo, query, since_date)
                )
                evidence.extend(await self._search_commits(http, repo, query))

        evidence.sort(key=lambda item: item.source_timestamp or since, reverse=True)
        return evidence[:limit]

    async def _search_issues_and_pulls(
        self, http: httpx.AsyncClient, repo: str, query: str, since_date: str
    ) -> list[EvidenceItem]:
        search_query = f"{query} repo:{repo} updated:>={since_date}"
        try:
            response = await http.get(
                "search/issues",
                params={
                    "q": search_query,
                    "sort": "updated",
                    "order": "desc",
                    "per_page": _PER_ENDPOINT_RESULTS,
                },
            )
            response.raise_for_status()
            items: list[dict[str, Any]] = response.json().get("items", [])
        except Exception as exc:
            logger.warning("github_live_search_issues_failed", repo=repo, error=str(exc))
            return []

        evidence: list[EvidenceItem] = []
        for item in items:
            is_pull_request = "pull_request" in item
            evidence.append(
                EvidenceItem(
                    source="pull_request" if is_pull_request else "issue",
                    reference=item.get("html_url") or f"{repo}#{item.get('number')}",
                    summary=self._build_summary(item.get("title") or "", item.get("body")),
                    retrieved_at=datetime.now(UTC),
                    source_timestamp=self._parse_timestamp(item.get("updated_at")),
                    metadata={
                        "repo": repo,
                        "kind": "pull_request" if is_pull_request else "issue",
                        "number": str(item.get("number", "")),
                        "author": (item.get("user") or {}).get("login", "unknown"),
                        "retrieval_mode": "live",
                    },
                )
            )
        return evidence

    async def _search_commits(
        self, http: httpx.AsyncClient, repo: str, query: str
    ) -> list[EvidenceItem]:
        search_query = f"{query} repo:{repo}"
        try:
            response = await http.get(
                "search/commits",
                params={
                    "q": search_query,
                    "sort": "author-date",
                    "order": "desc",
                    "per_page": _PER_ENDPOINT_RESULTS,
                },
            )
            response.raise_for_status()
            items: list[dict[str, Any]] = response.json().get("items", [])
        except Exception as exc:
            logger.warning("github_live_search_commits_failed", repo=repo, error=str(exc))
            return []

        evidence: list[EvidenceItem] = []
        for item in items:
            commit_info = item.get("commit", {})
            author_info = commit_info.get("author") or {}
            sha = item.get("sha", "")
            evidence.append(
                EvidenceItem(
                    source="commit",
                    reference=item.get("html_url") or f"{repo}@{sha}",
                    summary=self._build_summary(commit_info.get("message") or "", None),
                    retrieved_at=datetime.now(UTC),
                    source_timestamp=self._parse_timestamp(author_info.get("date")),
                    metadata={
                        "repo": repo,
                        "kind": "commit",
                        "sha": sha,
                        "author": author_info.get("name") or "unknown",
                        "retrieval_mode": "live",
                    },
                )
            )
        return evidence

    @staticmethod
    def _build_summary(title: str, body: str | None) -> str:
        text = f"{title}\n\n{body}".strip() if body else title
        if len(text) > _SUMMARY_MAX_CHARS:
            return text[:_SUMMARY_MAX_CHARS] + "..."
        return text

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
