"""Notion workspace page connector (API version 2026-03-11)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from app.ingestion.schemas import FetchResult, RawDocument, ResolvedConnectorConfig


@dataclass
class _NotionClient:
    http: httpx.AsyncClient


class NotionConnector:
    source_name = "notion"
    requests_per_second = 3.0
    supports_resume_token = False

    async def authenticate(self, config: ResolvedConnectorConfig) -> _NotionClient:
        http = httpx.AsyncClient(
            base_url="https://api.notion.com/v1/",
            headers={
                "Authorization": f"Bearer {config.credential_ref}",
                "Notion-Version": "2026-03-11",
            },
            timeout=30.0,
        )
        try:
            (await http.get("users/me")).raise_for_status()
        except Exception:
            await http.aclose()
            raise
        return _NotionClient(http=http)

    async def fetch_batch(
        self, client: _NotionClient, *, since: datetime | None, cursor: str | None
    ) -> FetchResult:
        body: dict[str, Any] = {
            "filter": {"property": "object", "value": "page"},
            "sort": {"direction": "ascending", "timestamp": "last_edited_time"},
            "page_size": 100,
        }
        if cursor:
            body["start_cursor"] = cursor
        response = await client.http.post("search", json=body)
        response.raise_for_status()
        payload = response.json()
        items = []
        for page in payload.get("results", []):
            if page.get("in_trash") or page.get("archived"):
                continue
            if (
                since
                and page.get("last_edited_time")
                and datetime.fromisoformat(page["last_edited_time"].replace("Z", "+00:00")) <= since
            ):
                continue
            page["_content"] = await self._page_text(client.http, page["id"])
            items.append(page)
        return FetchResult(
            items=items,
            next_cursor=payload.get("next_cursor"),
            has_more=bool(payload.get("has_more")),
        )

    async def _page_text(self, http: httpx.AsyncClient, page_id: str) -> str:
        cursor = None
        lines: list[str] = []
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            response = await http.get(f"blocks/{page_id}/children", params=params)
            response.raise_for_status()
            payload = response.json()
            for block in payload.get("results", []):
                value = block.get(block.get("type", ""), {})
                text = "".join(part.get("plain_text", "") for part in value.get("rich_text", []))
                if text:
                    lines.append(text)
            if not payload.get("has_more"):
                break
            cursor = payload.get("next_cursor")
        return "\n".join(lines)

    def normalize(self, raw_item: Any) -> RawDocument:
        title = ""
        for prop in (raw_item.get("properties") or {}).values():
            if prop.get("type") == "title":
                title = "".join(x.get("plain_text", "") for x in prop.get("title", []))
                break
        return RawDocument(
            source=self.source_name,
            external_id=raw_item["id"],
            title=title or None,
            content=raw_item.get("_content") or title,
            source_url=raw_item.get("url"),
            metadata={"updated": raw_item.get("last_edited_time", ""), "kind": "page"},
        )

    async def close(self, client: _NotionClient) -> None:
        await client.http.aclose()
