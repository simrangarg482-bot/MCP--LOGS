"""ServiceNow Table API connector for incidents and knowledge articles."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.ingestion.schemas import FetchResult, RawDocument, ResolvedConnectorConfig
from app.ingestion.url_safety import assert_safe_connector_url


@dataclass
class _ServiceNowClient:
    http: httpx.AsyncClient
    tables: list[str]
    instance_url: str


class ServiceNowConnector:
    source_name = "servicenow"
    requests_per_second = 3.0
    supports_resume_token = False

    async def authenticate(self, config: ResolvedConnectorConfig) -> _ServiceNowClient:
        instance_url = config.config.get("instance_url", "").rstrip("/")
        if not instance_url:
            raise RuntimeError("ServiceNow connector config is missing required 'instance_url'")
        assert_safe_connector_url(instance_url)
        credential = config.credential_ref
        headers = {"Accept": "application/json"}
        headers["Authorization"] = (
            f"Basic {base64.b64encode(credential.encode()).decode()}"
            if ":" in credential
            else f"Bearer {credential}"
        )
        http = httpx.AsyncClient(base_url=f"{instance_url}/api/now/", headers=headers, timeout=30.0)
        tables = list(config.config.get("tables", ["incident", "kb_knowledge"]))
        try:
            (await http.get(f"table/{tables[0]}", params={"sysparm_limit": 1})).raise_for_status()
        except Exception:
            await http.aclose()
            raise
        return _ServiceNowClient(http=http, tables=tables, instance_url=instance_url)

    async def fetch_batch(
        self, client: _ServiceNowClient, *, since: datetime | None, cursor: str | None
    ) -> FetchResult:
        state = json.loads(cursor) if cursor else {"table_index": 0, "offset": 0}
        index, offset = int(state["table_index"]), int(state["offset"])
        if index >= len(client.tables):
            return FetchResult(items=[], next_cursor=None, has_more=False)
        table = client.tables[index]
        query = "ORDERBYsys_updated_on"
        if since:
            stamp = since.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
            query = f"sys_updated_on>{stamp}^{query}"
        response = await client.http.get(
            f"table/{table}",
            params={
                "sysparm_limit": 100,
                "sysparm_offset": offset,
                "sysparm_query": query,
                "sysparm_display_value": "true",
            },
        )
        response.raise_for_status()
        items = response.json().get("result", [])
        for item in items:
            item["_table"] = table
            item["_instance_url"] = client.instance_url
        if len(items) == 100:
            state["offset"] = offset + 100
            has_more = True
        else:
            state.update(table_index=index + 1, offset=0)
            has_more = index + 1 < len(client.tables)
        return FetchResult(
            items=items, next_cursor=json.dumps(state) if has_more else None, has_more=has_more
        )

    def normalize(self, raw_item: Any) -> RawDocument:
        title = (
            raw_item.get("short_description") or raw_item.get("number") or raw_item.get("sys_id")
        )
        fields = [
            raw_item.get(key, "")
            for key in ("description", "text", "resolution_notes", "close_notes", "work_notes")
        ]
        content = f"{title}\n\n" + "\n\n".join(str(v) for v in fields if v)
        table, sys_id = raw_item["_table"], raw_item["sys_id"]
        return RawDocument(
            source=self.source_name,
            external_id=f"{table}:{sys_id}",
            title=title,
            content=content,
            source_url=f"{raw_item['_instance_url']}/{table}.do?sys_id={sys_id}",
            metadata={
                "table": table,
                "number": raw_item.get("number", ""),
                "state": str(raw_item.get("state", "")),
                "updated": raw_item.get("sys_updated_on", ""),
            },
        )

    async def close(self, client: _ServiceNowClient) -> None:
        await client.http.aclose()
