"""Incidents ingestion connector -- closes EKIP audit finding 6 ("No real
'incidents' retrieval collection"). Structurally identical to its sibling
`app.ingestion.connectors.runbooks.RunbooksConnector`: its source is also
*internal* (this codebase's own `incidents` table via `core.incidents`), not
an external SaaS API, and it exists for the same reason -- re-ingesting
core-domain data through the ordinary ingestion pipeline (classify -> chunk
-> embed -> upsert) is this codebase's own established pattern for getting
something searchable that nothing else produces embeddable chunks for,
rather than inventing a second, parallel embedding path inside
`core.incidents.service` itself.

Where this connector differs from `RunbooksConnector`: it re-ingests
*incidents* (not postmortems) into a *new* `"incidents"` retrieval
collection (not `"documentation"`), because `agents.service.
search_similar_incidents` needs to search actual historical incident
records, not postmortem write-ups mixed in with READMEs and runbooks.
`retrieval.schemas.CollectionName`'s own comment documents why `"incidents"`
is reachable only via an explicit `collection="incidents"` call, never the
"search everything" default -- this connector's output is inert for every
existing caller until something asks for it by name.

Same two disclosed, deliberate design points as `RunbooksConnector` (see
that module's docstring for the full reasoning, not repeated here):
`fetch_batch` opens its own short-lived read session via `session_scope`
(the `Connector` protocol has no session parameter to thread through), and
importing `core.incidents.reads` rather than `core.incidents.service`
avoids `ingestion` transitively depending on `agents` through that module's
own deferred `generate_postmortem` import.

Expected `ResolvedConnectorConfig.config` shape for this source: `{}` (no
per-connector-config filtering options exist yet -- every non-deleted
incident for the organization is eligible, open or resolved alike; see
`normalize()`'s own docstring for why an incident without a resolution is
still indexed, not skipped). `credential_ref` is unused, same as
`RunbooksConnector`.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.incidents import reads as incidents_reads
from app.database.session import session_scope
from app.ingestion.schemas import FetchResult, RawDocument, ResolvedConnectorConfig
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity

logger = get_logger(__name__)

_PAGE_SIZE = 50


@dataclass
class _IncidentsClient:
    """What `IncidentsConnector.authenticate` returns as `AuthenticatedClient`
    -- see `RunbooksConnector._RunbooksClient`'s docstring for why this is
    just an `organization_id` (no external system to hold a real client
    for) plus a constructed `Identity`.
    """

    organization_id: uuid.UUID
    actor: Identity


class IncidentsConnector:
    """Fetches incidents for one organization and ingests them as searchable
    `"incidents"`-collection content, so `agents.service.
    search_similar_incidents` (and the Retrieval Agent's own
    `historical_similarity` confidence signal) can actually search real
    historical incident records instead of unrelated documentation/code/
    conversation chunks.
    """

    source_name = "incidents"
    # No external API -- see `RunbooksConnector.requests_per_second`'s
    # identical reasoning: this connector's only "request" is a local
    # database query, not a throttled outbound HTTP call.
    requests_per_second = 100.0

    async def authenticate(self, config: ResolvedConnectorConfig) -> _IncidentsClient:
        """No external system to authenticate against -- see
        `RunbooksConnector.authenticate`'s identical reasoning.
        """
        actor = Identity.for_agent("ingestion_worker", config.organization_id)
        logger.info(
            "incidents_authenticate_succeeded",
            connector_config_id=str(config.connector_config_id),
            organization_id=str(config.organization_id),
        )
        return _IncidentsClient(organization_id=config.organization_id, actor=actor)

    async def fetch_batch(
        self,
        client: _IncidentsClient,
        *,
        since: datetime | None,
        cursor: str | None,
    ) -> FetchResult:
        """Fetch one page of incidents for `client.organization_id`, each
        paired with its approved/published postmortem's `root_cause` (or
        `None`), oldest-last-modified-first.

        `cursor` is this connector's own opaque JSON envelope
        `{"offset": int}` -- see `RunbooksConnector.fetch_batch`'s identical
        reasoning for why this opens its own short-lived session.
        """
        offset = self._decode_cursor(cursor)

        async with session_scope() as session:
            pairs = await incidents_reads.list_incidents_for_ingestion(
                session,
                client.organization_id,
                since=since,
                offset=offset,
                limit=_PAGE_SIZE,
            )

        items = [
            {**incident.model_dump(mode="json"), "root_cause": root_cause}
            for incident, root_cause in pairs
        ]

        has_more = len(items) == _PAGE_SIZE
        next_state = {"offset": offset + len(items)}

        return FetchResult(
            items=items,
            next_cursor=json.dumps(next_state) if has_more else None,
            has_more=has_more,
        )

    def normalize(self, raw_item: Any) -> RawDocument:
        """Convert one raw incident dict (an `Incident.model_dump(mode=
        "json")` plus a `root_cause` key) into a `RawDocument`.

        Content is title + description + resolution -- the "most useful
        incident fields" for similarity search: the title is often the most
        semantically dense summary of the symptoms (e.g. "checkout
        returning 500 errors"), the description gives the full report, and
        the resolution (an approved/published postmortem's `root_cause`,
        when one exists) is what actually distinguishes "this happened
        before" from "here's what fixed it." `root_cause` is deliberately
        OPTIONAL, never required: most incidents will be synced before a
        postmortem exists at all (postmortems are written after the fact,
        often well after an incident closes, per `core.incidents.service.
        trigger_postmortem_generation`'s own flow), and requiring one would
        mean never indexing the very "recent, still-open, no postmortem
        yet" incidents a live triage call most needs to compare against.
        `repository.list_incidents_for_ingestion`'s `since`-filtering (on
        whichever of the incident/its postmortem was modified more
        recently) re-syncs this same incident again once a resolution does
        arrive, so this is a "day one, minimal text" choice, not a
        permanent gap.
        """
        incident_id = raw_item["id"]
        title = raw_item["title"]
        description = raw_item["description"]
        root_cause = raw_item.get("root_cause")

        content = f"{title}\n\n{description}"
        if root_cause:
            content = f"{content}\n\nResolution: {root_cause}"

        metadata: dict[str, str] = {
            "incident_id": str(incident_id),
            "status": raw_item["status"],
            "severity": raw_item["severity"],
        }

        return RawDocument(
            source=self.source_name,
            external_id=str(incident_id),
            content=content,
            title=title,
            source_url=f"/incidents/{incident_id}",
            metadata=metadata,
        )

    async def close(self, client: _IncidentsClient) -> None:
        """No-op -- `authenticate` opened no external connection to close."""
        return None

    @staticmethod
    def _decode_cursor(cursor: str | None) -> int:
        """Parse this connector's opaque cursor envelope back into
        `offset`, defaulting to 0 when `cursor` is None -- same convention
        as `RunbooksConnector._decode_cursor`.
        """
        if cursor is None:
            return 0
        state = json.loads(cursor)
        return int(state["offset"])
