"""Google Drive document connector using Drive API v3."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.ingestion.office_extraction import extract_text
from app.ingestion.schemas import FetchResult, RawDocument, ResolvedConnectorConfig

_BASE = "https://www.googleapis.com/drive/v3/"
_PAGE_SIZE = 100
_GOOGLE_EXPORTS = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
    "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
}


@dataclass
class _GoogleDriveClient:
    http: httpx.AsyncClient
    folder_ids: list[str]


class GoogleDriveConnector:
    source_name = "google_drive"
    requests_per_second = 5.0
    supports_resume_token = False

    async def authenticate(self, config: ResolvedConnectorConfig) -> _GoogleDriveClient:
        http = httpx.AsyncClient(
            base_url=_BASE,
            headers={"Authorization": f"Bearer {config.credential_ref}"},
            timeout=30.0,
        )
        try:
            (await http.get("about", params={"fields": "user"})).raise_for_status()
        except Exception:
            await http.aclose()
            raise
        return _GoogleDriveClient(http=http, folder_ids=list(config.config.get("folder_ids", [])))

    async def fetch_batch(
        self, client: _GoogleDriveClient, *, since: datetime | None, cursor: str | None
    ) -> FetchResult:
        state = json.loads(cursor) if cursor else {"folder_index": 0, "page_token": None}
        folders = client.folder_ids or [None]
        index = int(state["folder_index"])
        if index >= len(folders):
            return FetchResult(items=[], next_cursor=None, has_more=False)
        clauses = ["trashed = false"]
        if folders[index]:
            clauses.append(f"'{folders[index]}' in parents")
        if since:
            clauses.append(
                f"modifiedTime > '{since.astimezone(UTC).isoformat().replace('+00:00', 'Z')}'"
            )
        params: dict[str, Any] = {
            "q": " and ".join(clauses),
            "pageSize": _PAGE_SIZE,
            "fields": (
                "nextPageToken,files(id,name,mimeType,modifiedTime,webViewLink,"
                "parents,owners(displayName))"
            ),
            "orderBy": "modifiedTime",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if state.get("page_token"):
            params["pageToken"] = state["page_token"]
        response = await client.http.get("files", params=params)
        response.raise_for_status()
        payload = response.json()
        items = []
        for item in payload.get("files", []):
            content = await self._download(client.http, item)
            if content:
                item["_content"] = content
                items.append(item)
        next_page = payload.get("nextPageToken")
        next_index = index if next_page else index + 1
        has_more = bool(next_page) or next_index < len(folders)
        next_state = {"folder_index": next_index, "page_token": next_page}
        return FetchResult(
            items=items, next_cursor=json.dumps(next_state) if has_more else None, has_more=has_more
        )

    async def _download(self, http: httpx.AsyncClient, item: dict[str, Any]) -> str | None:
        mime = item.get("mimeType", "")
        if mime in _GOOGLE_EXPORTS:
            export_mime, extension = _GOOGLE_EXPORTS[mime]
            response = await http.get(
                f"files/{item['id']}/export", params={"mimeType": export_mime}
            )
            filename = item.get("name", "document") + extension
        else:
            response = await http.get(f"files/{item['id']}", params={"alt": "media"})
            filename = item.get("name", "")
        if response.status_code in {403, 404}:
            return None
        response.raise_for_status()
        extracted = extract_text(filename, response.content)
        if extracted is not None:
            return extracted
        if mime.startswith("text/") or filename.lower().endswith(
            (".md", ".txt", ".csv", ".json", ".yaml", ".yml")
        ):
            return response.content.decode("utf-8", errors="replace")
        return None

    def normalize(self, raw_item: Any) -> RawDocument:
        owners = raw_item.get("owners") or []
        metadata = {
            "mime_type": raw_item.get("mimeType", ""),
            "modified": raw_item.get("modifiedTime", ""),
        }
        if owners:
            metadata["owner"] = owners[0].get("displayName", "")
        return RawDocument(
            source=self.source_name,
            external_id=raw_item["id"],
            title=raw_item.get("name"),
            content=raw_item["_content"],
            source_url=raw_item.get("webViewLink"),
            metadata=metadata,
        )

    async def close(self, client: _GoogleDriveClient) -> None:
        await client.http.aclose()
