"""PagerDuty incidents connector using the REST API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.ingestion.schemas import FetchResult, RawDocument, ResolvedConnectorConfig


@dataclass
class _PagerDutyClient:
    http: httpx.AsyncClient
    service_ids: list[str]


class PagerDutyConnector:
    source_name = "pagerduty"
    requests_per_second = 5.0
    supports_resume_token = False

    async def authenticate(self, config: ResolvedConnectorConfig) -> _PagerDutyClient:
        http = httpx.AsyncClient(
            base_url="https://api.pagerduty.com/",
            headers={
                "Authorization": f"Token token={config.credential_ref}",
                "Accept": "application/vnd.pagerduty+json;version=2",
            },
            timeout=30.0,
        )
        try:
            (await http.get("users", params={"limit": 1})).raise_for_status()
        except Exception:
            await http.aclose()
            raise
        return _PagerDutyClient(http=http, service_ids=list(config.config.get("service_ids", [])))

    async def fetch_batch(
        self, client: _PagerDutyClient, *, since: datetime | None, cursor: str | None
    ) -> FetchResult:
        offset = int(cursor or 0)
        params: list[tuple[str, Any]] = [
            ("limit", 100),
            ("offset", offset),
            ("sort_by", "created_at:asc"),
        ]
        if since:
            params.append(("since", since.astimezone(UTC).isoformat()))
        for service_id in client.service_ids:
            params.append(("service_ids[]", service_id))
        response = await client.http.get("incidents", params=params)
        response.raise_for_status()
        payload = response.json()
        items = payload.get("incidents", [])
        has_more = bool(payload.get("more"))
        return FetchResult(
            items=items,
            next_cursor=str(offset + len(items)) if has_more else None,
            has_more=has_more,
        )

    def normalize(self, raw_item: Any) -> RawDocument:
        service = raw_item.get("service") or {}
        urgency = raw_item.get("urgency", "")
        content = "\n".join(
            [
                raw_item.get("title", ""),
                f"Status: {raw_item.get('status', '')}",
                f"Urgency: {urgency}",
                f"Service: {service.get('summary', '')}",
                f"Created: {raw_item.get('created_at', '')}",
                f"Resolved: {raw_item.get('resolved_at') or ''}",
            ]
        )
        return RawDocument(
            source=self.source_name,
            external_id=raw_item["id"],
            title=raw_item.get("title"),
            content=content,
            source_url=raw_item.get("html_url"),
            metadata={
                "status": raw_item.get("status", ""),
                "urgency": urgency,
                "service": service.get("summary", ""),
                "created": raw_item.get("created_at", ""),
            },
        )

    async def close(self, client: _PagerDutyClient) -> None:
        await client.http.aclose()
