"""Live monitoring/alerting evidence source -- still mocked (always returns
no evidence), but now genuinely reachable, closing a previously-flagged gap.

Implemented as a real `LiveEvidenceSource`, not just left as the bare
function `_gather_monitoring_evidence` already is, so that a future
PagerDuty/Datadog/Grafana/etc. integration is a drop-in replacement for this
one class -- swap `MonitoringLiveSource` for a real implementation and the
Investigation Agent's evidence-gathering code does not change at all.

Now registered in `agents.investigation.evidence._LIVE_SOURCES` under the
`"monitoring"` key, matching the now-added `ConnectorSource` value
(`app.core.tenancy.schemas.ConnectorSource`) and `EvidenceItem.source` value
(`app.shared.schemas.agent_contracts.EvidenceItem`). A caller can therefore
register a `connector_configs` row with `source="monitoring"` (via
`POST /tenancy/connectors` or the MCP tool-driven equivalent) and have
`_gather_live_evidence` actually dispatch to this class for it -- previously
impossible, since no `ConnectorSource` value existed to route through at
all. `fetch_live_evidence` still always returns `[]`: no real monitoring/
alerting API integration exists yet (that remains its own, separate,
larger piece of work -- this change closes the "unreachable" gap, not the
"no real backend" one). `connector_config.config` (the same free-form JSONB
dict every other connector's registration already carries) is where a real
future implementation would read e.g. an API base URL/query template from --
already plumbed through via this method's `connector_config` parameter, so
no interface change is needed when that real backend is built.
"""

from __future__ import annotations

from datetime import datetime

from app.core.tenancy.schemas import ConnectorConfig
from app.shared.schemas import EvidenceItem


class MonitoringLiveSource:
    """Always returns no evidence -- see module docstring."""

    source_name = "monitoring"
    requests_per_second = 1.0

    async def fetch_live_evidence(
        self,
        *,
        connector_config: ConnectorConfig,
        query: str,
        since: datetime,
        limit: int,
    ) -> list[EvidenceItem]:
        return []
