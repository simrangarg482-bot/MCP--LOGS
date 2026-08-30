"""Contract smoke tests for the second-wave enterprise connectors."""

from app.ingestion.connectors.base import Connector
from app.ingestion.connectors.gitlab import GitLabConnector
from app.ingestion.connectors.google_drive import GoogleDriveConnector
from app.ingestion.connectors.notion import NotionConnector
from app.ingestion.connectors.pagerduty import PagerDutyConnector
from app.ingestion.connectors.servicenow import ServiceNowConnector
from app.ingestion.processors.pipeline import process_document


def _assert_enters_shared_pipeline(document) -> None:
    processed = process_document(document)
    assert processed.source == document.source
    assert processed.external_id == document.external_id
    assert processed.chunks
    assert processed.content_hash


def test_all_enterprise_connectors_implement_protocol() -> None:
    connectors = [
        GoogleDriveConnector(),
        GitLabConnector(),
        NotionConnector(),
        ServiceNowConnector(),
        PagerDutyConnector(),
    ]
    for connector in connectors:
        assert isinstance(connector, Connector)
        assert connector.requests_per_second > 0


def test_google_drive_normalizes_a_downloaded_file() -> None:
    document = GoogleDriveConnector().normalize(
        {
            "id": "drive-1",
            "name": "Operations runbook",
            "mimeType": "text/plain",
            "modifiedTime": "2026-08-30T10:00:00Z",
            "webViewLink": "https://drive.google.com/file/d/drive-1/view",
            "owners": [{"displayName": "Operations"}],
            "_content": "Restart the worker after draining the queue.",
        }
    )
    assert document.external_id == "drive-1"
    assert document.metadata["owner"] == "Operations"
    assert "draining the queue" in document.content
    _assert_enters_shared_pipeline(document)


def test_gitlab_normalizes_issue_with_stable_project_scoped_id() -> None:
    document = GitLabConnector().normalize(
        {
            "iid": 42,
            "title": "Checkout timeout",
            "description": "The payment adapter exceeded five seconds.",
            "web_url": "https://gitlab.example/acme/payments/-/issues/42",
            "state": "opened",
            "labels": ["incident"],
            "updated_at": "2026-08-30T10:00:00Z",
            "_project": "acme/payments",
            "_kind": "issue",
        }
    )
    assert document.external_id == "acme/payments:issue:42"
    assert document.metadata["labels"] == "incident"
    _assert_enters_shared_pipeline(document)


def test_notion_normalizes_page_title_and_blocks() -> None:
    document = NotionConnector().normalize(
        {
            "id": "page-1",
            "url": "https://notion.so/page-1",
            "last_edited_time": "2026-08-30T10:00:00Z",
            "properties": {"Name": {"type": "title", "title": [{"plain_text": "On-call guide"}]}},
            "_content": "Escalate severity-one incidents to the incident commander.",
        }
    )
    assert document.title == "On-call guide"
    assert document.metadata["kind"] == "page"
    _assert_enters_shared_pipeline(document)


def test_servicenow_normalizes_incident() -> None:
    document = ServiceNowConnector().normalize(
        {
            "sys_id": "abc",
            "number": "INC001",
            "short_description": "Database pool exhausted",
            "description": "Authentication requests timed out.",
            "state": "Resolved",
            "sys_updated_on": "2026-08-30 10:00:00",
            "_table": "incident",
            "_instance_url": "https://acme.service-now.com",
        }
    )
    assert document.external_id == "incident:abc"
    assert "Authentication requests timed out" in document.content
    _assert_enters_shared_pipeline(document)


def test_pagerduty_normalizes_incident() -> None:
    document = PagerDutyConnector().normalize(
        {
            "id": "PD1",
            "title": "API latency",
            "status": "resolved",
            "urgency": "high",
            "service": {"summary": "Checkout API"},
            "created_at": "2026-08-30T10:00:00Z",
            "resolved_at": "2026-08-30T10:10:00Z",
            "html_url": "https://acme.pagerduty.com/incidents/PD1",
        }
    )
    assert document.external_id == "PD1"
    assert "Checkout API" in document.content
    _assert_enters_shared_pipeline(document)
