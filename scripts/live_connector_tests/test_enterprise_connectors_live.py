"""Credential-gated live smoke tests for the second-wave connectors."""

from __future__ import annotations

import uuid

import pytest

from app.ingestion.connectors.gitlab import GitLabConnector
from app.ingestion.connectors.google_drive import GoogleDriveConnector
from app.ingestion.connectors.notion import NotionConnector
from app.ingestion.connectors.pagerduty import PagerDutyConnector
from app.ingestion.connectors.servicenow import ServiceNowConnector
from app.ingestion.schemas import ResolvedConnectorConfig


async def _exercise(connector, spec, organization_id: uuid.UUID) -> None:
    config = ResolvedConnectorConfig(
        connector_config_id=uuid.uuid4(),
        organization_id=organization_id,
        project_id=None,
        source=connector.source_name,
        credential_ref=spec.credential_ref,
        config=spec.config,
    )
    client = await connector.authenticate(config)
    try:
        page = await connector.fetch_batch(client, since=None, cursor=None)
        for item in page.items[:3]:
            document = connector.normalize(item)
            assert document.source == connector.source_name
            assert document.external_id
    finally:
        await connector.close(client)


@pytest.mark.parametrize(
    ("fixture_name", "connector"),
    [
        ("google_drive_spec", GoogleDriveConnector()),
        ("gitlab_spec", GitLabConnector()),
        ("notion_spec", NotionConnector()),
        ("servicenow_spec", ServiceNowConnector()),
        ("pagerduty_spec", PagerDutyConnector()),
    ],
)
async def test_enterprise_connector_live(request, organization_id, fixture_name, connector) -> None:
    await _exercise(connector, request.getfixturevalue(fixture_name), organization_id)
