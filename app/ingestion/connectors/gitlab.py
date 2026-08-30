"""GitLab issues and merge-request connector for SaaS or self-managed GitLab."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.ingestion.schemas import FetchResult, RawDocument, ResolvedConnectorConfig
from app.ingestion.url_safety import assert_safe_connector_url


@dataclass
class _GitLabClient:
    http: httpx.AsyncClient
    projects: list[str]
    base_url: str


class GitLabConnector:
    source_name = "gitlab"
    requests_per_second = 5.0
    supports_resume_token = False

    async def authenticate(self, config: ResolvedConnectorConfig) -> _GitLabClient:
        base_url = config.config.get("base_url", "https://gitlab.com").rstrip("/")
        assert_safe_connector_url(base_url)
        http = httpx.AsyncClient(
            base_url=f"{base_url}/api/v4/",
            headers={"PRIVATE-TOKEN": config.credential_ref},
            timeout=30.0,
        )
        try:
            (await http.get("user")).raise_for_status()
        except Exception:
            await http.aclose()
            raise
        return _GitLabClient(
            http=http, projects=list(config.config.get("projects", [])), base_url=base_url
        )

    async def fetch_batch(
        self, client: _GitLabClient, *, since: datetime | None, cursor: str | None
    ) -> FetchResult:
        state = json.loads(cursor) if cursor else {"project_index": 0, "kind_index": 0, "page": 1}
        kinds = ("issues", "merge_requests")
        pi, ki, page = int(state["project_index"]), int(state["kind_index"]), int(state["page"])
        if pi >= len(client.projects):
            return FetchResult(items=[], next_cursor=None, has_more=False)
        project, kind = client.projects[pi], kinds[ki]
        params: dict[str, Any] = {
            "scope": "all",
            "state": "all",
            "per_page": 100,
            "page": page,
            "order_by": "updated_at",
            "sort": "asc",
        }
        if since:
            params["updated_after"] = since.astimezone(UTC).isoformat()
        response = await client.http.get(
            f"projects/{quote(project, safe='')}/{kind}", params=params
        )
        response.raise_for_status()
        items = response.json()
        for item in items:
            item["_project"] = project
            item["_kind"] = kind.rstrip("s")
        next_page = response.headers.get("x-next-page")
        if next_page:
            state["page"] = int(next_page)
        elif ki + 1 < len(kinds):
            state.update(kind_index=ki + 1, page=1)
        else:
            state.update(project_index=pi + 1, kind_index=0, page=1)
        has_more = bool(next_page) or ki + 1 < len(kinds) or pi + 1 < len(client.projects)
        return FetchResult(
            items=items, next_cursor=json.dumps(state) if has_more else None, has_more=has_more
        )

    def normalize(self, raw_item: Any) -> RawDocument:
        notes = raw_item.get("description") or ""
        labels = raw_item.get("labels") or []
        content = f"{raw_item.get('title', '')}\n\n{notes}"
        return RawDocument(
            source=self.source_name,
            external_id=f"{raw_item['_project']}:{raw_item['_kind']}:{raw_item['iid']}",
            title=raw_item.get("title"),
            content=content,
            source_url=raw_item.get("web_url"),
            metadata={
                "project": raw_item["_project"],
                "kind": raw_item["_kind"],
                "state": raw_item.get("state", ""),
                "labels": ",".join(labels),
                "updated": raw_item.get("updated_at", ""),
            },
        )

    async def close(self, client: _GitLabClient) -> None:
        await client.http.aclose()
