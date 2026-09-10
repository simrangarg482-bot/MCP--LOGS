"""Registers an "incidents" connector for this organization, the same way
the real app would via POST /connectors -- calls the actual
`tenancy_service.register_connector` function (proper credential
encryption, audit log entry) rather than a raw INSERT. This is the internal,
self-ingesting connector documented in
`app.ingestion.connectors.incidents.IncidentsConnector`: it has no external
credentials (the "incidents" table it reads is this app's own), so
credential_ref is a harmless placeholder string that the connector never
reads back.

There is currently no frontend UI to do this (the "Connect a source" modal
only lists external SaaS sources), which is why this connector has never
existed for any organization -- confirmed via check_internal_connectors.py.

    python create_incidents_connector.py
"""

from __future__ import annotations

import asyncio
import uuid

from app.core.tenancy import service as tenancy_service
from app.core.tenancy.schemas import ConnectorConfigCreate
from app.database.session import session_scope, set_tenant_context
from app.shared.schemas import ActorKind, Identity

_ORGANIZATION_ID = uuid.UUID("602b7004-3749-440b-a59b-36ff11036db5")


async def main() -> None:
    async with session_scope() as session:
        await set_tenant_context(session, _ORGANIZATION_ID)

        # A synthetic admin identity for this one-off script -- grants only
        # the single permission register_connector actually checks
        # (tenancy:manage), scoped to this organization. Not a bypass of
        # anything in production; this script runs with the same DB
        # credentials that already have full read/write access to this org's
        # data.
        actor = Identity(
            kind=ActorKind.USER,
            subject="manual-setup-script",
            organization_id=_ORGANIZATION_ID,
            permissions=frozenset({"tenancy:manage"}),
        )

        result = await tenancy_service.register_connector(
            session,
            actor,
            _ORGANIZATION_ID,
            ConnectorConfigCreate(
                source="incidents",
                credential_ref="unused-internal-source",
                config={},
            ),
        )
        print(f"Created connector: id={result.id} source={result.source} status={result.status}")


if __name__ == "__main__":
    asyncio.run(main())
