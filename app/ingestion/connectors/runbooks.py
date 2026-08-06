"""Runbooks/incident-report ingestion connector -- the last item in
PROJECT_PLAN.md section 14's Milestone 9 connector list, and structurally
unlike every other connector in this package: its source is *internal*
(this codebase's own `postmortems` table via `core.incidents`), not an
external SaaS API.

Closes a real, previously-flagged gap: `agents.investigation.evidence`'s own
docstring already notes "no 'postmortems' retrieval collection exists yet,"
so an approved postmortem is only ever surfaced to the Investigation Agent
via a direct, non-ranked recency query (`core.incidents.service.
list_recent_postmortems`) -- never through the Answer Agent's normal hybrid
search, and never for a *different* incident's investigation than the
handful `list_recent_postmortems` happens to return. Running this connector
embeds every approved/published postmortem into the `documentation`
collection (the same collection `core.knowledge.service.publish_document`
already uses for published runbooks -- see that module's docstring on why
no dedicated "postmortems" collection exists yet either), making them
reachable by ordinary retrieval search, citations, and the Knowledge Gap
Agent's `related_document_id` resolution alike.

Two real, disclosed architectural gaps this connector's design works around
rather than papers over:

1. **`Connector.fetch_batch`/`authenticate` take no `AsyncSession`.** Every
   other connector in this package needs none -- their I/O is all outbound
   HTTP. This connector's "fetch" is a database read, and the `Connector`
   protocol (designed around external APIs) has nowhere to thread a session
   through. Rather than changing that protocol (a much larger, riskier
   change touching all six connectors and `ingestion/service.py`'s calling
   convention), this connector opens its own short-lived read session via
   `app.database.session.session_scope` inside `fetch_batch` itself. This is
   safe specifically because this connector only *reads* -- every *write*
   this sync produces (the resulting `Document`/chunks) still goes through
   the normal outer-session/savepoint machinery in
   `ingestion.service._execute_ingestion_job`, untouched.

2. **`ingestion` importing `core.incidents` is a new, undocumented
   dependency edge**, exactly like the pre-existing, already-flagged
   `ingestion -> core.tenancy` edge `ingestion.service`'s own module
   docstring discusses (for `connector_config` access) -- PROJECT_PLAN.md
   section 9.8 lists ingestion's dependencies as retrieval/database/shared
   only. No import-linter contract forbids it (`pyproject.toml`'s
   "ingestion does not depend on agents or mcp" contract names only those
   two modules), so this is a documented gap in the plan, not a violation of
   an enforced rule.

Expected `ResolvedConnectorConfig.config` shape for this source: `{}` (no
per-connector-config filtering options exist yet -- every approved/
published postmortem for the organization is eligible). `credential_ref` is
unused (there is no external credential to hold), left as whatever
placeholder value an admin's `connector_configs` row happens to carry for
this source.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.incidents import service as incidents_service
from app.database.session import session_scope
from app.ingestion.schemas import FetchResult, RawDocument, ResolvedConnectorConfig
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity

logger = get_logger(__name__)

_PAGE_SIZE = 50


@dataclass
class _RunbooksClient:
    """What `RunbooksConnector.authenticate` returns as `AuthenticatedClient`.

    Just an `organization_id` -- there is no external system to hold a real
    client for (see module docstring). `Identity.for_agent(...)` is
    constructed once here rather than per `fetch_batch` call, mirroring
    `ingestion.service._execute_ingestion_job`'s own single
    `Identity.for_agent("ingestion_worker", organization_id)` construction
    for the same underlying reason: a system job, not a per-request human
    caller (though this connector's read path doesn't actually need an
    actor at all -- `list_postmortems_for_ingestion` takes none, see that
    function's docstring -- kept here only in case a future permission
    boundary is added to that read).
    """

    organization_id: uuid.UUID
    actor: Identity


class RunbooksConnector:
    """Fetches approved/published postmortems for one organization and
    re-ingests them as searchable `documentation`-collection content.
    """

    source_name = "runbooks"
    # No external API, so no real rate limit applies -- a nominal, generous
    # value (this connector's only "request" is a local database query, not
    # a throttled outbound HTTP call the worker pool needs to budget for the
    # way it does for every other connector in this package).
    requests_per_second = 100.0

    async def authenticate(self, config: ResolvedConnectorConfig) -> _RunbooksClient:
        """No external system to authenticate against -- unlike every other
        connector's `authenticate`, this makes no network call at all, just
        bundles `organization_id` (and a constructed `Identity`) for
        `fetch_batch` to use.
        """
        actor = Identity.for_agent("ingestion_worker", config.organization_id)
        logger.info(
            "runbooks_authenticate_succeeded",
            connector_config_id=str(config.connector_config_id),
            organization_id=str(config.organization_id),
        )
        return _RunbooksClient(organization_id=config.organization_id, actor=actor)

    async def fetch_batch(
        self,
        client: _RunbooksClient,
        *,
        since: datetime | None,
        cursor: str | None,
    ) -> FetchResult:
        """Fetch one page of approved/published postmortems for
        `client.organization_id`.

        `cursor` is this connector's own opaque JSON envelope
        `{"offset": int}` -- see module docstring's point 2 on why this
        opens its own short-lived session rather than receiving one.
        """
        offset = self._decode_cursor(cursor)

        async with session_scope() as session:
            postmortems = await incidents_service.list_postmortems_for_ingestion(
                session,
                client.organization_id,
                since=since,
                offset=offset,
                limit=_PAGE_SIZE,
            )

        items = [postmortem.model_dump(mode="json") for postmortem in postmortems]

        has_more = len(items) == _PAGE_SIZE
        next_state = {"offset": offset + len(items)}

        return FetchResult(
            items=items,
            next_cursor=json.dumps(next_state) if has_more else None,
            has_more=has_more,
        )

    def normalize(self, raw_item: Any) -> RawDocument:
        """Convert one raw postmortem dict (a `Postmortem.model_dump(mode=
        "json")`) into a `RawDocument`.

        Content is synthesized from `root_cause` + `action_items` -- a
        postmortem has no single free-text body the way a Jira issue's
        description or a Confluence page's storage-format body does, so this
        is this connector's own interpretive step (still `normalize`'s job,
        not the processing pipeline's -- PROJECT_PLAN.md section 4.1).
        """
        postmortem_id = raw_item["id"]
        incident_id = raw_item["incident_id"]
        root_cause = raw_item.get("root_cause") or "(no root cause recorded)"
        action_items = raw_item.get("action_items") or []

        lines = [f"Root cause: {root_cause}"]
        if action_items:
            lines.append("")
            lines.append("Action items:")
            for item in action_items:
                owner_suffix = f" (owner: {item['owner']})" if item.get("owner") else ""
                lines.append(f"- [{item['status']}] {item['description']}{owner_suffix}")
        content = "\n".join(lines)

        metadata: dict[str, str] = {
            "incident_id": str(incident_id),
            "status": raw_item["status"],
            "generated_by": raw_item["generated_by"],
        }
        if raw_item.get("updated_at"):
            metadata["updated"] = raw_item["updated_at"]

        return RawDocument(
            source=self.source_name,
            external_id=str(postmortem_id),
            content=content,
            title=f"Postmortem: incident {incident_id}",
            source_url=None,
            metadata=metadata,
        )

    async def close(self, client: _RunbooksClient) -> None:
        """No-op -- `authenticate` opened no external connection to close."""
        return None

    @staticmethod
    def _decode_cursor(cursor: str | None) -> int:
        """Parse this connector's opaque cursor envelope back into
        `offset`, defaulting to 0 when `cursor` is None (a full sync's first
        page, or an incremental sync's first page for this run).
        """
        if cursor is None:
            return 0
        state = json.loads(cursor)
        return int(state["offset"])
