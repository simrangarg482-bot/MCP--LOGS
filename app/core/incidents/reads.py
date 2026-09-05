"""A deliberately narrow, agents-free read surface on top of
`core.incidents.repository`, for callers outside `core/incidents` that must
never pull in `core.incidents.service`'s transitive `app.agents.service`
dependency (its deferred import inside `generate_postmortem`, needed to break
a genuine circular import between `agents` and `core.incidents` -- see that
function's own docstring).

`app.ingestion.connectors.runbooks` was the first caller: it needs
read-only access to approved/published postmortems to re-ingest them as
searchable content, and nothing else `core.incidents.service` offers.
Importing `core.incidents.service` for that alone previously made
`ingestion` transitively depend on `agents`, breaking the "ingestion does
not depend on agents or mcp" import-linter contract -- Python has no way to
import only part of a module, so even an unused, function-scoped import
elsewhere in that file still counts.

`app.ingestion.connectors.incidents.IncidentsConnector` is the second
caller, for the analogous reason: read-only access to incidents (title +
description + resolution) to re-ingest them into the `"incidents"`
retrieval collection.

`core.incidents.service` re-exports both `list_postmortems_for_ingestion`
and `list_incidents_for_ingestion` from here (rather than duplicating them)
so every other existing caller keeps working unchanged.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.incidents import repository
from app.core.incidents.schemas import Incident, Postmortem


async def list_postmortems_for_ingestion(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    since: datetime | None,
    offset: int,
    limit: int,
) -> list[Postmortem]:
    """Return approved/published postmortems for `organization_id`,
    oldest-first, offset-paginated -- backs the Milestone 9 runbooks/
    incident-report ingestion connector (`app.ingestion.connectors.runbooks`),
    the source `agents.investigation.evidence`'s own docstring already flags
    as missing ("no 'postmortems' retrieval collection exists yet").

    Deliberately no `actor: Identity` parameter -- a rare, explicit exception
    to `core.incidents.service`'s "every function takes an actor" rule, in
    the same spirit as `core.tenancy.service.list_organizations`/
    `get_organization_sso_config`: the caller is `ingestion`'s worker, a
    scheduled system job with no per-request human identity (see
    `ingestion.service`'s own module docstring: it already constructs
    `Identity.for_agent("ingestion_worker", organization_id)` purely for
    audit-tagging, not for permission checks -- there is no permission check
    to gate here either, matching `list_recent_postmortems`'s "read-only
    lookup, no require_permission gate" precedent).
    """
    rows = await repository.list_postmortems_for_ingestion(
        session,
        organization_id,
        statuses=("approved", "published"),
        since=since,
        offset=offset,
        limit=limit,
    )
    return [Postmortem.model_validate(row) for row in rows]


async def list_incidents_for_ingestion(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    since: datetime | None,
    offset: int,
    limit: int,
) -> list[tuple[Incident, str | None]]:
    """Return `(incident, resolution_root_cause)` pairs for
    `organization_id`, oldest-last-modified-first, offset-paginated --
    backs `app.ingestion.connectors.incidents.IncidentsConnector`. See
    `repository.list_incidents_for_ingestion`'s own docstring for the
    resolution join and the `updated_at`-vs-`created_at` ordering choice.

    Same "no `actor: Identity` parameter" exception as
    `list_postmortems_for_ingestion` above, for the identical reason: the
    caller is `ingestion`'s worker, a scheduled system job, not a
    per-request human identity.
    """
    rows = await repository.list_incidents_for_ingestion(
        session, organization_id, since=since, offset=offset, limit=limit
    )
    return [(Incident.model_validate(row.Incident), row.root_cause) for row in rows]
