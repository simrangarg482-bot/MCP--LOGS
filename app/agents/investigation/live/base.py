"""`LiveEvidenceSource` -- the protocol every live, targeted evidence source
the Investigation Agent can query directly during an active investigation
implements (`github_live.py`, `slack_live.py`, `monitoring_live.py`).

Structurally parallel to `app.ingestion.connectors.base.Connector`, but
deliberately a *separate*, simpler protocol, not a reuse of that one: a live
lookup is a single-shot, narrow, incident-scoped query ("give me what's
relevant right now" -- a handful of requests, no pagination cursor, no
persistence, no chunk/embed pipeline), fundamentally different from a full,
resumable, paginated sync destined for `retrieval.upsert`
(`ingestion.processors.pipeline`). Keeping this protocol here, inside
`agents/investigation/`, rather than importing or extending anything under
`app.ingestion`, is also what keeps `agents/` honoring the import-linter
contract that forbids it from depending on `app.ingestion` at all
(`pyproject.toml`'s `[tool.importlinter]`, "agents does not depend on mcp or
ingestion internals") -- ingestion remains the *only* place that owns the
long-term fetch-clean-chunk-embed-store pipeline; a `LiveEvidenceSource`
only ever produces `EvidenceItem`s directly, never a `RawDocument`, and
never writes anything to `documents`/`document_metadata`/any chunk table.

Every live source is handed the already-resolved `ConnectorConfig`
(`app.core.tenancy.schemas.ConnectorConfig` -- core/tenancy's own read
model, not ingestion's `app.ingestion.schemas.ResolvedConnectorConfig`) for
the specific connector it's querying, so it can read
`connector_config.credential_ref` / `connector_config.config` (repos,
channels, ...) the same way the equivalent ingestion connector does,
without `agents/` needing to know anything about `app.ingestion`'s internal
shapes. `agents.investigation.evidence._gather_live_evidence` is the only
caller that resolves which `ConnectorConfig` rows exist (via
`core.tenancy.service.list_connectors`) and dispatches to the matching
`LiveEvidenceSource` by `connector_config.source`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.core.tenancy.schemas import ConnectorConfig
from app.shared.schemas import EvidenceItem


@runtime_checkable
class LiveEvidenceSource(Protocol):
    """One external source the Investigation Agent can query live. See
    module docstring.
    """

    source_name: str

    async def fetch_live_evidence(
        self,
        *,
        connector_config: ConnectorConfig,
        query: str,
        since: datetime,
        limit: int,
    ) -> list[EvidenceItem]:
        """Return up to `limit` fresh `EvidenceItem`s relevant to `query`, no
        older than `since`, fetched directly from the external API -- never
        from anything already ingested/indexed by `retrieval.search()`.

        Every returned item's `metadata` must include
        `"retrieval_mode": "live"` (see
        `agents.investigation.evidence._gather_live_evidence`'s docstring
        for why: it is how sub-stage B's hypothesis prompt --
        `agents.investigation.hypothesis._format_evidence_line`, unchanged
        by this feature -- and any future reader of `EvidenceItem` can tell
        this apart from indexed evidence, without a dedicated schema field).

        Must not raise for a single failed sub-request (e.g. one repo's
        search failing, one channel's history call failing) -- that should
        be logged and skipped internally, the same "an individual source's
        failure is logged and skipped, never fatal" discipline
        `agents.investigation.evidence`'s module docstring already applies
        to every other evidence source. It is acceptable to let a *total*
        failure (e.g. an invalid credential) raise, since the caller already
        wraps every `fetch_live_evidence` call in `agents.retry.
        call_with_retry` plus its own try/except, exactly like every other
        source in this module.
        """
        ...
