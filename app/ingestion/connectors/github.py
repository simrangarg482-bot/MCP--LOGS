"""GitHub connector -- one of the first two sources for Milestone 4
(PROJECT_PLAN.md section 4: "recommend starting with Slack and GitHub" --
representative of the "code" content type), extended to also fetch commits,
pull requests, and issues (the Investigation Agent's evidence-gathering
step, `app.agents.investigation.evidence`, needed these to stop being a
documented, flagged gap).

Implements `app.ingestion.connectors.base.Connector` structurally, matching
`SlackConnector`'s approach: uses `httpx` directly against GitHub's REST API
(no `PyGithub` dependency, for the same "httpx is already a core dependency,
don't add a second HTTP client for one connector" reasoning as Slack).

Expected `ResolvedConnectorConfig.config` shape for this source (unchanged):
    {"repos": [{"repo": "owner/name", "ref": "main"}, ...]}
`ref` is optional per repo entry, defaulting to `"main"`.

**Four kinds of raw item, one connector.** Each repo is walked through four
phases in order -- `"files"` (unchanged existing behavior) -> `"commits"` ->
`"pulls"` -> `"issues"` -- before `fetch_batch` moves on to the next repo.
The cursor carries this phase alongside the existing `repo_index`/`page`
envelope (`{"repo_index": int, "phase": str, "page": int}`), staying fully
stateless between calls (see "Sync strategy" below for why). Every raw item
this connector produces carries an internal `"_kind"` discriminator
(`"file"` | `"commit"` | `"pull_request"` | `"issue"`), read by `normalize()`
to dispatch to the matching builder -- `_kind` never appears on the
resulting `RawDocument` itself, only in the connector-internal dict shape
`FetchResult.items` carries (see `base.Connector.fetch_batch`'s docstring:
raw items are source-native, `normalize()`-only knowledge).

Every non-file `RawDocument`'s `metadata` always includes `"kind"` (one of
`"commit"`/`"pull_request"`/`"issue"`) plus `"repo"` -- this is the exact key
`agents.investigation.evidence._chunk_to_evidence` reads (via
`ScoredChunk.metadata`, populated when a caller passes
`retrieval.search(..., include_metadata=True)`) to tell a GitHub file
apart from a commit/PR/issue chunk. Kind-specific metadata keys beyond that:
  - `commit`: `sha`, `author`, `timestamp` (commit's own authored date),
    `changed_files` (comma-joined filenames).
  - `pull_request`: `number`, `author`, `timestamp` (`created_at`),
    `merged_at` (present only if merged), `changed_files` (comma-joined
    filenames from `/pulls/{n}/files`), `reviews` (comma-joined
    `"reviewer:state"` pairs -- a deliberately shallow "if feasible"
    summary, not full review body text).
  - `issue`: `number`, `author`, `timestamp` (`created_at`), `labels`
    (comma-joined label names), `closed_at` (present only if closed),
    `comments_count`. Comment *bodies* are appended into `content` itself
    (see `_normalize_issue`), not stashed in metadata, so they are actually
    searchable/retrievable text, not just a count.
All metadata values are plain strings (`document_metadata.value` is `TEXT`,
and `RawDocument.metadata` is typed `dict[str, str]`) -- lists (changed
files, labels) are comma-joined, never a nested structure.

**Titles are embedded into `content`, not left only on `RawDocument.title`,
for commits/PRs/issues.** A chunk's embedding is computed from `content`
alone (`ingestion.processors.pipeline`) -- `RawDocument.title` only ever
becomes the `documents.title` display column, never part of what gets
embedded. An issue/PR whose *title* is the single most information-dense
line (e.g. "Login fails after SSO redirect") would be unsearchable by title
text alone if it lived only in `title`; embedding it as the first line of
`content` too (`f"{title}\n\n{body}"`) is what makes it retrievable at all.

Sync strategy differs by whether this is a full or incremental sync, since
GitHub has no single endpoint for "everything, but only changed since X"
that covers all four kinds uniformly:
  - **Files**, full sync (`since=None`): walks the repo's Git tree at `ref`
    (`GET /repos/{repo}/git/trees/{ref}?recursive=1`), fetching every
    non-binary file's current content. Incremental (`since=<ts>`): walks
    commits since that timestamp, collects the set of files touched,
    fetches each file's *current* content. **Unchanged from before this
    extension** -- see `_list_tree_page`/`_list_changed_paths_page`.
  - **Commits**: `GET /repos/{repo}/commits` supports `since` natively
    (server-side filter) for both full and incremental sync; each commit's
    detail (`GET /repos/{repo}/commits/{sha}`) is fetched for its message,
    author, and changed-files list.
  - **Pull requests**: `GET /repos/{repo}/pulls` has **no** `since`
    parameter at all. Incremental sync is implemented as a client-side
    cutoff instead: pages are requested `sort=updated&direction=desc`, and
    as soon as one page's PRs cross below `since`, the rest of that page
    (and every later page) is skipped -- correct because "sorted by updated,
    descending" guarantees everything after the first too-old PR is also
    too old.
  - **Issues**: `GET /repos/{repo}/issues` *does* support `since` natively,
    same as commits. Its response also includes every pull request in the
    repo (a PR is a specialization of an issue in GitHub's own data model)
    -- entries carrying a `"pull_request"` key are filtered out here, since
    the `pulls` phase already covers those with PR-specific enrichment
    (changed files, reviews) issues don't have.

Known limitations, flagged rather than silently built around (in addition
to the pre-existing ones below, still true for the `files` phase):
  - **Commit details are fetched twice during an incremental sync that
    touches both the `files` and `commits` phases** -- once by
    `_list_changed_paths_page` (to discover which files changed) and again
    by `_list_commits_page` (to build commit evidence), both via the same
    `_fetch_commit_detail` helper. Not cached across phases: this
    connector's stateless-between-calls design (a job's cursor must carry
    all resumable state, since a retry may run on a different worker
    process) means there is nowhere safe to stash a cross-phase cache
    without growing the cursor into something a lot more complex than a
    `{repo_index, phase, page}` envelope. Accepted as a real, bounded cost
    (extra requests against the same 5,000/hour budget), not silently
    optimized around.
  - File content is fetched one file at a time, sequentially, inside a
    single `fetch_batch` call (as is resolving each incremental commit's
    changed-files list, and each PR's/issue's enrichment calls) -- an N+1
    pattern against GitHub's REST API. Fine at this connector's current
    page sizes; a real throughput need would want bounded-concurrency
    `asyncio.gather` here instead.
  - Incremental pagination (files, commits, and the PR cutoff scan) uses a
    page-length heuristic ("fewer than a full page means this was the last
    one") rather than parsing GitHub's `Link` response header, which is the
    API's authoritative pagination signal.
  - Files over ~1MB come back from the Contents API with no inline content
    (GitHub requires the Blobs API or a raw download for those) -- such
    files are silently skipped, the same as genuinely binary files.
  - A full sync (`since=None`) fetches *all* commits/PRs/issues in a repo's
    history, bounded per call by page size but not by any time window --
    for a very old, high-activity repository this is a real, potentially
    large first-sync cost. No max-lookback config knob is added here
    (that would be a new abstraction this task doesn't require); flagged as
    a follow-up if it becomes a real operational problem.
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

_GITHUB_API_BASE_URL = "https://api.github.com/"
_GITHUB_API_VERSION = "2022-11-28"
_TREE_PAGE_SIZE = 50  # files per page when walking a full tree
_COMMITS_PAGE_SIZE = 50  # commits per page when walking history
_PULLS_PAGE_SIZE = 30  # PRs per page -- smaller than commits: each PR costs
# three follow-up calls (detail, files, reviews), so a smaller page keeps
# one fetch_batch call more bounded time-wise.
_ISSUES_PAGE_SIZE = 30  # same reasoning as pulls (each issue may cost a
# follow-up comments call).

# The order every repo's phases are walked in, per `fetch_batch` call.
# "files" first preserves this connector's pre-existing behavior taking
# priority (a caller relying only on file sync sees no change in when its
# data shows up); commits/pulls/issues are additive phases appended after.
_PHASES: tuple[str, ...] = ("files", "commits", "pulls", "issues")

# Mechanical fetch-time filter, not a business judgment about "importance"
# (that stays in the processing pipeline, PROJECT_PLAN.md section 4.1) --
# fetching these as text content would just produce garbage.
_SKIP_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pdf", ".zip", ".tar",
    ".gz", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mov", ".exe", ".dll",
    ".so", ".bin", ".lock",
}

# Dependency caches and generated build trees are both extremely large and
# low-value for enterprise knowledge search. Repositories sometimes commit
# them accidentally (the live 21-repository sync exposed a complete
# `.venv_mac/.../site-packages` tree), turning one useful project into
# thousands of third-party documents. Match path segments, not substrings,
# so names such as `environment.py` remain eligible.
_SKIP_PATH_SEGMENTS = {
    "__pycache__",
    ".cache",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "site-packages",
    "target",
}


@dataclass
class _RepoConfig:
    repo: str  # "owner/name"
    ref: str


@dataclass
class _GitHubClient:
    """What `GitHubConnector.authenticate` returns as `AuthenticatedClient`.

    Bundles the authenticated HTTP client with the fixed repo list --
    `fetch_batch` only receives this object back, not the original config,
    same reasoning as `SlackConnector`'s `_SlackClient`.
    """

    http: httpx.AsyncClient
    repos: list[_RepoConfig]


class GitHubConnector:
    """Fetches file contents, commits, pull requests, and issues from a
    fixed set of GitHub repositories -- see module docstring.
    """

    source_name = "github"
    # GitHub's REST API allows 5,000 requests/hour for an authenticated
    # (PAT or installation) token -- a hair under 1.4 req/s steady-state;
    # 1.0 req/s is a conservative budget that leaves headroom for other
    # connector_configs sharing the same worker pool (section 4.5). Still
    # true after this extension: more calls happen per `fetch_batch`
    # invocation now (commit/PR/issue enrichment), but the ceiling this
    # connector declares to the worker pool's rate limiter is unchanged.
    requests_per_second = 1.0

    async def authenticate(self, config: ResolvedConnectorConfig) -> _GitHubClient:
        """Build an authenticated GitHub client from `config`.

        `config.credential_ref` is the plaintext PAT/installation token,
        already decrypted by `app.ingestion.service` via `shared/security`
        before this connector ever sees it -- see `base.Connector.
        authenticate`'s docstring; this connector never stores or logs the
        token itself, only forwards it as a bearer header on its own
        outbound requests. Calls `GET /rate_limit` once
        (does not itself count against the rate limit) so an invalid token
        fails loudly here rather than on the first real fetch.
        """
        http = httpx.AsyncClient(
            base_url=_GITHUB_API_BASE_URL,
            headers={
                "Authorization": f"Bearer {config.credential_ref}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            },
            timeout=30.0,
        )
        try:
            response = await http.get("rate_limit")
            response.raise_for_status()
        except Exception:
            await http.aclose()
            logger.warning(
                "github_authenticate_failed",
                connector_config_id=str(config.connector_config_id),
            )
            raise

        repos = [
            _RepoConfig(repo=entry["repo"], ref=entry.get("ref", "main"))
            for entry in config.config.get("repos", [])
        ]
        logger.info(
            "github_authenticate_succeeded",
            connector_config_id=str(config.connector_config_id),
            repo_count=len(repos),
        )
        return _GitHubClient(http=http, repos=repos)

    async def fetch_batch(
        self,
        client: _GitHubClient,
        *,
        since: datetime | None,
        cursor: str | None,
    ) -> FetchResult:
        """Fetch one page of raw items -- of whichever kind the current
        phase is -- from `client.repos`.

        `cursor` is this connector's own opaque JSON envelope
        `{"repo_index": int, "phase": str, "page": int}` -- see this
        module's docstring for why the phase list is re-derived from
        scratch each call rather than cached connector-side, and for the
        phase order.
        """
        repo_index, phase, page = self._decode_cursor(cursor)

        if repo_index >= len(client.repos):
            return FetchResult(items=[], next_cursor=None, has_more=False)

        repo_config = client.repos[repo_index]

        if phase == "files":
            items, exhausted = await self._list_file_items_page(
                client.http, repo_config, since, page
            )
        elif phase == "commits":
            items, exhausted = await self._list_commits_page(client.http, repo_config, since, page)
        elif phase == "pulls":
            items, exhausted = await self._list_pull_requests_page(
                client.http, repo_config, since, page
            )
        else:  # "issues"
            items, exhausted = await self._list_issues_page(client.http, repo_config, since, page)

        if exhausted:
            next_phase = self._next_phase(phase)
            if next_phase is not None:
                next_state = {"repo_index": repo_index, "phase": next_phase, "page": 0}
                has_more = True
            else:
                next_repo_index = repo_index + 1
                has_more = next_repo_index < len(client.repos)
                next_state = {"repo_index": next_repo_index, "phase": _PHASES[0], "page": 0}
        else:
            has_more = True
            next_state = {"repo_index": repo_index, "phase": phase, "page": page + 1}

        return FetchResult(
            items=items,
            next_cursor=json.dumps(next_state) if has_more else None,
            has_more=has_more,
        )

    def normalize(self, raw_item: Any) -> RawDocument:
        """Convert one raw GitHub item (of whichever kind) into a
        `RawDocument`, dispatching on the internal `"_kind"` discriminator
        `fetch_batch` attached.
        """
        kind = raw_item["_kind"]
        if kind == "file":
            return self._normalize_file(raw_item)
        if kind == "commit":
            return self._normalize_commit(raw_item)
        if kind == "pull_request":
            return self._normalize_pull_request(raw_item)
        if kind == "issue":
            return self._normalize_issue(raw_item)
        raise ValueError(f"Unknown GitHub raw item kind: {kind!r}")  # pragma: no cover - defensive

    async def close(self, client: _GitHubClient) -> None:
        """Close the underlying `httpx.AsyncClient` opened by `authenticate`."""
        await client.http.aclose()

    # --- files (unchanged behavior; only relocated into a small wrapper to
    # fit the phase-dispatch shape `fetch_batch` now uses) -----------------

    async def _list_file_items_page(
        self,
        http: httpx.AsyncClient,
        repo_config: _RepoConfig,
        since: datetime | None,
        page: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Full-sync (tree walk) or incremental (commit-diff-derived) file
        listing, then fetch each listed path's current content -- exactly
        the pre-existing file-sync behavior, untouched.
        """
        if since is None:
            paths, exhausted = await self._list_tree_page(http, repo_config, page)
        else:
            paths, exhausted = await self._list_changed_paths_page(http, repo_config, since, page)

        items: list[dict[str, Any]] = []
        for path in paths:
            content = await self._fetch_file_content(http, repo_config, path)
            if content is None:
                continue  # binary, oversized, or undecodable -- skipped, not an error
            items.append(
                {
                    "_kind": "file",
                    "_repo": repo_config.repo,
                    "_ref": repo_config.ref,
                    "path": path,
                    "content": content,
                }
            )
        return items, exhausted

    async def _list_tree_page(
        self, http: httpx.AsyncClient, repo_config: _RepoConfig, page: int
    ) -> tuple[list[str], bool]:
        """Full-sync path: fetch the whole recursive tree once, slice it
        into `_TREE_PAGE_SIZE`-sized pages. Cheap to re-fetch (paths + SHAs
        only, no blob content) on every call, given this connector's
        stateless-between-calls design (see module docstring).
        """
        response = await http.get(
            f"repos/{repo_config.repo}/git/trees/{repo_config.ref}", params={"recursive": "1"}
        )
        response.raise_for_status()
        payload = response.json()
        all_paths = [
            entry["path"]
            for entry in payload.get("tree", [])
            if entry.get("type") == "blob" and not self._is_skipped(entry["path"])
        ]
        start = page * _TREE_PAGE_SIZE
        end = start + _TREE_PAGE_SIZE
        page_paths = all_paths[start:end]
        exhausted = end >= len(all_paths)
        return page_paths, exhausted

    async def _list_changed_paths_page(
        self,
        http: httpx.AsyncClient,
        repo_config: _RepoConfig,
        since: datetime,
        page: int,
    ) -> tuple[list[str], bool]:
        """Incremental-sync path: list commits since `since`, one page of
        commits at a time, then resolve each commit's changed file paths
        (via `_fetch_commit_detail`). Fetches *current* file content
        afterward (in `_list_file_items_page`), not the historical diff --
        see module docstring.
        """
        response = await http.get(
            f"repos/{repo_config.repo}/commits",
            params={
                "sha": repo_config.ref,
                "since": since.astimezone(timezone.utc).isoformat(),
                "per_page": _COMMITS_PAGE_SIZE,
                "page": page + 1,  # GitHub pages are 1-indexed
            },
        )
        response.raise_for_status()
        commits = response.json()

        paths: set[str] = set()
        for commit in commits:
            detail = await self._fetch_commit_detail(http, repo_config, commit["sha"])
            for file_entry in detail.get("files", []):
                if not self._is_skipped(file_entry["filename"]):
                    paths.add(file_entry["filename"])

        # Length-based heuristic, not GitHub's `Link` header -- see this
        # module's "Known limitations" note.
        exhausted = len(commits) < _COMMITS_PAGE_SIZE
        return list(paths), exhausted

    async def _fetch_file_content(
        self, http: httpx.AsyncClient, repo_config: _RepoConfig, path: str
    ) -> str | None:
        """Fetch one file's current text content, or None if it can't be
        treated as inline UTF-8 text (binary, oversized, or otherwise not
        inline-fetchable) -- skipped, not an error.
        """
        response = await http.get(
            f"repos/{repo_config.repo}/contents/{path}", params={"ref": repo_config.ref}
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("encoding") != "base64" or not payload.get("content"):
            return None
        try:
            return base64.b64decode(payload["content"]).decode("utf-8")
        except Exception:
            # Any decode failure (bad padding, non-UTF-8 bytes, ...) is
            # treated the same as "this file isn't ingestable text", not a
            # reason to fail the whole batch.
            return None

    def _normalize_file(self, raw_item: dict[str, Any]) -> RawDocument:
        """Convert one raw GitHub file item into a `RawDocument`. Identical
        to this connector's pre-extension `normalize()` body -- existing
        behavior for files is unchanged.
        """
        repo = raw_item["_repo"]
        ref = raw_item["_ref"]
        path = raw_item["path"]
        return RawDocument(
            source=self.source_name,
            external_id=f"{repo}:{path}",
            content=raw_item["content"],
            title=path,
            source_url=f"https://github.com/{repo}/blob/{ref}/{path}",
            metadata={"repo": repo, "path": path, "ref": ref},
        )

    # --- commits -----------------------------------------------------------

    async def _fetch_commit_detail(
        self, http: httpx.AsyncClient, repo_config: _RepoConfig, sha: str
    ) -> dict[str, Any]:
        """Fetch one commit's full detail (message, author, changed files).
        Shared by both the `files` phase's incremental-diff discovery and
        the `commits` phase's evidence-gathering -- see module docstring's
        "known limitations" entry on why this means the same commit's
        detail can be fetched twice within one job, not cached across
        phases.
        """
        response = await http.get(f"repos/{repo_config.repo}/commits/{sha}")
        response.raise_for_status()
        return response.json()

    async def _list_commits_page(
        self,
        http: httpx.AsyncClient,
        repo_config: _RepoConfig,
        since: datetime | None,
        page: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        """List one page of commits (`GET /repos/{repo}/commits`, which
        supports `since` natively for both full and incremental sync),
        enriching each with its message/author/changed-files via
        `_fetch_commit_detail`.
        """
        params: dict[str, str | int] = {
            "sha": repo_config.ref,
            "per_page": _COMMITS_PAGE_SIZE,
            "page": page + 1,
        }
        if since is not None:
            params["since"] = since.astimezone(timezone.utc).isoformat()

        response = await http.get(f"repos/{repo_config.repo}/commits", params=params)
        response.raise_for_status()
        commits = response.json()

        items: list[dict[str, Any]] = []
        for commit in commits:
            sha = commit["sha"]
            detail = await self._fetch_commit_detail(http, repo_config, sha)
            commit_info = detail.get("commit", {})
            author_info = commit_info.get("author") or {}
            changed_files = [
                entry["filename"]
                for entry in detail.get("files", [])
                if not self._is_skipped(entry["filename"])
            ]
            items.append(
                {
                    "_kind": "commit",
                    "_repo": repo_config.repo,
                    "_ref": repo_config.ref,
                    "sha": sha,
                    "message": commit_info.get("message", ""),
                    "author": author_info.get("name") or author_info.get("email") or "unknown",
                    "timestamp": author_info.get("date") or "",
                    "changed_files": ",".join(changed_files),
                }
            )

        exhausted = len(commits) < _COMMITS_PAGE_SIZE
        return items, exhausted

    def _normalize_commit(self, raw_item: dict[str, Any]) -> RawDocument:
        """Convert one raw commit item into a `RawDocument`. `content` is
        the commit message itself (matching `SlackConnector.normalize`'s
        "content is pure text, facts go in metadata" convention) -- author/
        timestamp/changed-files live in `metadata`, not baked into `content`.
        """
        repo = raw_item["_repo"]
        sha = raw_item["sha"]
        message = raw_item["message"]
        title = message.splitlines()[0][:200] if message else None
        return RawDocument(
            source=self.source_name,
            external_id=f"{repo}@{sha}",
            content=message,
            title=title,
            source_url=f"https://github.com/{repo}/commit/{sha}",
            metadata={
                "repo": repo,
                "kind": "commit",
                "sha": sha,
                "author": raw_item["author"],
                "timestamp": raw_item["timestamp"],
                "changed_files": raw_item["changed_files"],
            },
        )

    # --- pull requests -------------------------------------------------------

    async def _list_pull_requests_page(
        self,
        http: httpx.AsyncClient,
        repo_config: _RepoConfig,
        since: datetime | None,
        page: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        """List one page of pull requests, sorted by `updated` descending.

        `GET /repos/{repo}/pulls` has no `since` parameter -- incremental
        sync is a client-side cutoff instead: as soon as a PR older than
        `since` is seen, everything remaining in this (descending-sorted)
        page and every later page is guaranteed older too, so the scan
        stops there and this page counts as exhausted. See module
        docstring's "Sync strategy" section.
        """
        response = await http.get(
            f"repos/{repo_config.repo}/pulls",
            params={
                "state": "all",
                "sort": "updated",
                "direction": "desc",
                "per_page": _PULLS_PAGE_SIZE,
                "page": page + 1,
            },
        )
        response.raise_for_status()
        pulls = response.json()

        since_utc = since.astimezone(timezone.utc) if since is not None else None

        items: list[dict[str, Any]] = []
        cutoff_reached = False
        for pull in pulls:
            if since_utc is not None:
                updated_at = self._parse_github_timestamp(pull["updated_at"])
                if updated_at < since_utc:
                    cutoff_reached = True
                    break
            items.append(await self._build_pull_request_item(http, repo_config, pull))

        exhausted = cutoff_reached or len(pulls) < _PULLS_PAGE_SIZE
        return items, exhausted

    async def _build_pull_request_item(
        self, http: httpx.AsyncClient, repo_config: _RepoConfig, pull: dict[str, Any]
    ) -> dict[str, Any]:
        """Enrich one PR list-entry with the fields the list response
        doesn't carry: `merged_at` (from the single-PR detail endpoint),
        changed file paths, and a shallow review summary.
        """
        number = pull["number"]
        detail_response = await http.get(f"repos/{repo_config.repo}/pulls/{number}")
        detail_response.raise_for_status()
        detail = detail_response.json()

        changed_files = await self._list_pull_request_files(http, repo_config, number)
        reviews = await self._list_pull_request_reviews(http, repo_config, number)

        return {
            "_kind": "pull_request",
            "_repo": repo_config.repo,
            "_ref": repo_config.ref,
            "number": number,
            "title": pull.get("title") or "",
            "body": pull.get("body") or "",
            "author": (pull.get("user") or {}).get("login", "unknown"),
            "html_url": pull.get("html_url", ""),
            "created_at": pull.get("created_at", ""),
            "merged_at": detail.get("merged_at") or "",
            "changed_files": ",".join(changed_files),
            "reviews": reviews,
        }

    async def _list_pull_request_files(
        self, http: httpx.AsyncClient, repo_config: _RepoConfig, number: int
    ) -> list[str]:
        """Filenames changed by PR `number` (`GET /pulls/{n}/files`)."""
        response = await http.get(f"repos/{repo_config.repo}/pulls/{number}/files")
        response.raise_for_status()
        return [entry["filename"] for entry in response.json()]

    async def _list_pull_request_reviews(
        self, http: httpx.AsyncClient, repo_config: _RepoConfig, number: int
    ) -> str:
        """A deliberately shallow "if feasible" review summary -- comma-
        joined `"reviewer:state"` pairs, not full review body text (see
        module docstring). Returns `""` if the PR has no reviews.
        """
        response = await http.get(f"repos/{repo_config.repo}/pulls/{number}/reviews")
        response.raise_for_status()
        reviews = response.json()
        return ",".join(
            f"{(review.get('user') or {}).get('login', 'unknown')}:{review.get('state', '')}"
            for review in reviews
        )

    def _normalize_pull_request(self, raw_item: dict[str, Any]) -> RawDocument:
        """Convert one raw PR item into a `RawDocument`. `content` is
        `title` + `body` combined -- see module docstring's "titles are
        embedded into content" note on why the title can't live only in
        `RawDocument.title`.
        """
        repo = raw_item["_repo"]
        number = raw_item["number"]
        title = raw_item["title"]
        content = f"{title}\n\n{raw_item['body']}".strip()

        metadata = {
            "repo": repo,
            "kind": "pull_request",
            "number": str(number),
            "author": raw_item["author"],
            "timestamp": raw_item["created_at"],
            "changed_files": raw_item["changed_files"],
        }
        if raw_item.get("merged_at"):
            metadata["merged_at"] = raw_item["merged_at"]
        if raw_item.get("reviews"):
            metadata["reviews"] = raw_item["reviews"]

        return RawDocument(
            source=self.source_name,
            external_id=f"{repo}#pull-{number}",
            content=content,
            title=title,
            source_url=raw_item["html_url"],
            metadata=metadata,
        )

    # --- issues --------------------------------------------------------------

    async def _list_issues_page(
        self,
        http: httpx.AsyncClient,
        repo_config: _RepoConfig,
        since: datetime | None,
        page: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        """List one page of issues. `GET /repos/{repo}/issues` supports
        `since` natively (unlike `/pulls`) for both full and incremental
        sync.

        GitHub's `/issues` endpoint also returns every pull request in the
        repo (a PR is a specialization of an issue) -- entries carrying a
        `"pull_request"` key are excluded here, since the `pulls` phase
        already covers those with PR-specific enrichment. Filtering these
        out means a page can come back smaller than `_ISSUES_PAGE_SIZE`
        even when more issues remain -- the exhaustion check below is
        against the *raw*, pre-filter page length, not the filtered count,
        to keep the "short page means last page" heuristic correct.
        """
        params: dict[str, str | int] = {
            "state": "all",
            "sort": "updated",
            "direction": "desc",
            "per_page": _ISSUES_PAGE_SIZE,
            "page": page + 1,
        }
        if since is not None:
            params["since"] = since.astimezone(timezone.utc).isoformat()

        response = await http.get(f"repos/{repo_config.repo}/issues", params=params)
        response.raise_for_status()
        raw_issues = response.json()

        true_issues = [issue for issue in raw_issues if "pull_request" not in issue]
        items = [
            await self._build_issue_item(http, repo_config, issue) for issue in true_issues
        ]

        exhausted = len(raw_issues) < _ISSUES_PAGE_SIZE
        return items, exhausted

    async def _build_issue_item(
        self, http: httpx.AsyncClient, repo_config: _RepoConfig, issue: dict[str, Any]
    ) -> dict[str, Any]:
        """Enrich one issue list-entry with its comment text, if it has
        any (`GET /issues/{n}/comments`) -- skipped entirely when
        `comments == 0`, avoiding a wasted call for the common case.
        """
        number = issue["number"]
        comments_count = issue.get("comments", 0)
        comments_text = (
            await self._fetch_issue_comments_text(http, repo_config, number)
            if comments_count
            else ""
        )

        return {
            "_kind": "issue",
            "_repo": repo_config.repo,
            "_ref": repo_config.ref,
            "number": number,
            "title": issue.get("title") or "",
            "body": issue.get("body") or "",
            "author": (issue.get("user") or {}).get("login", "unknown"),
            "html_url": issue.get("html_url", ""),
            "labels": ",".join(label.get("name", "") for label in issue.get("labels", [])),
            "created_at": issue.get("created_at", ""),
            "closed_at": issue.get("closed_at") or "",
            "comments_count": str(comments_count),
            "comments_text": comments_text,
        }

    async def _fetch_issue_comments_text(
        self, http: httpx.AsyncClient, repo_config: _RepoConfig, number: int
    ) -> str:
        """Concatenate every comment on issue `number` into one text block
        (`"author: body"` per comment, blank-line separated) -- appended
        into `content` by `_normalize_issue`, so comments are actually
        searchable/retrievable, not just counted.
        """
        response = await http.get(f"repos/{repo_config.repo}/issues/{number}/comments")
        response.raise_for_status()
        comments = response.json()
        return "\n\n".join(
            f"{(comment.get('user') or {}).get('login', 'unknown')}: {comment.get('body', '')}"
            for comment in comments
        )

    def _normalize_issue(self, raw_item: dict[str, Any]) -> RawDocument:
        """Convert one raw issue item into a `RawDocument`. `content` is
        `title` + `body`, with any comment text appended after a clear
        delimiter -- see module docstring's "titles are embedded into
        content" note.
        """
        repo = raw_item["_repo"]
        number = raw_item["number"]
        title = raw_item["title"]
        content = f"{title}\n\n{raw_item['body']}".strip()
        if raw_item.get("comments_text"):
            content = f"{content}\n\n--- Comments ---\n\n{raw_item['comments_text']}"

        metadata = {
            "repo": repo,
            "kind": "issue",
            "number": str(number),
            "author": raw_item["author"],
            "timestamp": raw_item["created_at"],
            "comments_count": raw_item["comments_count"],
        }
        if raw_item.get("labels"):
            metadata["labels"] = raw_item["labels"]
        if raw_item.get("closed_at"):
            metadata["closed_at"] = raw_item["closed_at"]

        return RawDocument(
            source=self.source_name,
            external_id=f"{repo}#issue-{number}",
            content=content,
            title=title,
            source_url=raw_item["html_url"],
            metadata=metadata,
        )

    # --- shared helpers --------------------------------------------------------

    @staticmethod
    def _is_skipped(path: str) -> bool:
        normalized_path = path.replace("\\", "/").lower()
        segments = normalized_path.split("/")
        in_generated_tree = any(
            segment in _SKIP_PATH_SEGMENTS
            or segment == "venv"
            or segment.startswith(".venv")
            for segment in segments[:-1]
        )
        return in_generated_tree or any(
            normalized_path.endswith(extension) for extension in _SKIP_EXTENSIONS
        )

    @staticmethod
    def _parse_github_timestamp(value: str) -> datetime:
        """Parse a GitHub API timestamp (ISO 8601, `Z`-suffixed UTC) into an
        aware `datetime`. The explicit `Z` -> `+00:00` swap is defensive,
        not strictly required on this project's pinned Python (3.11+
        already parses a trailing `Z` in `fromisoformat`), but keeps this
        correct regardless of exact patch-version parsing nuances.
        """
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _next_phase(phase: str) -> str | None:
        """The phase after `phase` in `_PHASES`, or `None` if `phase` is
        the last one (meaning this repo is fully exhausted).
        """
        index = _PHASES.index(phase)
        return _PHASES[index + 1] if index + 1 < len(_PHASES) else None

    @staticmethod
    def _decode_cursor(cursor: str | None) -> tuple[int, str, int]:
        """Parse this connector's opaque cursor envelope back into
        `(repo_index, phase, page)`, defaulting to the first repo's first
        phase's first page when `cursor` is None.
        """
        if cursor is None:
            return 0, _PHASES[0], 0
        state = json.loads(cursor)
        return int(state["repo_index"]), str(state["phase"]), int(state["page"])
