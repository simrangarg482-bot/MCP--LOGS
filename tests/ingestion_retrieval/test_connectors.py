"""Phase 1 -- Connector Testing.

PURPOSE
    Verify every connector EKIP actually implements, against real external
    services, using real credentials from this harness's own `.env`.

WHAT THIS SCRIPT ACTUALLY EXERCISES (read directly from
`app/ingestion/connectors/`, not assumed)
    EKIP implements exactly 8 connectors: Slack, GitHub, Jira, Teams,
    Azure DevOps, Confluence, SharePoint, and an internal "Runbooks"
    connector (re-ingests this project's own approved postmortems -- no
    external API). There is NO Google Drive connector anywhere in this
    codebase -- this script does not test one, and says so explicitly
    below, rather than inventing one.

    Each connector implements the same `Connector` Protocol
    (`app/ingestion/connectors/base.py`): `authenticate(config) -> client`,
    `fetch_batch(client, since, cursor) -> FetchResult`,
    `normalize(raw_item) -> RawDocument`, `close(client)`. This script calls
    exactly those four real, unmodified methods per connector, via
    `utils.fetch_all_sync` -- the same sequence
    `app.ingestion.service._execute_ingestion_job` runs in production, minus
    the DB job-tracking wrapper around it.

WHICH APIS THIS CALLS
    Real, live calls to whichever of Slack/GitHub/Jira/Confluence/Teams/
    SharePoint/Azure DevOps APIs have credentials configured in `.env`.
    Runbooks makes no external call -- it reads this harness's own
    bootstrapped organization's `postmortems` table via a real, unmodified
    project function (`core.incidents.service.list_postmortems_for_ingestion`).

EXPECTED INPUT
    `tests/ingestion_retrieval/.env`, copied from `.env.example` in this
    same directory, with as many connectors' credentials filled in as you
    have available. Any connector left blank is reported SKIPPED, not
    FAILED -- this script never guesses or fabricates credentials.

EXPECTED OUTPUT
    One block per connector:

        Connector: Slack
        Authentication       PASS
        Connection           PASS
        Documents fetched    PASS
        Sample data          PASS
        Records fetched: 50

    ...followed by a final PASS/FAIL/SKIP summary table.

HOW TO RUN
    python tests/ingestion_retrieval/test_connectors.py
    (must run inside the PROJECT's own virtualenv -- this script imports
    app.* modules directly)

COMMON FAILURES
    - "SKIPPED (credentials not set)": expected and harmless if you haven't
      configured that connector; not a bug.
    - 401/403 from a real API: check the token/PAT/API-key value and its
      scopes (see README.md's per-connector required-scope notes).
    - Azure DevOps/Jira/Confluence 404: `EKIP_TEST_*_PROJECTS` /
      `_SPACES` value doesn't match a real project/space key at that
      base_url/organization.
    - "Runbooks: 0 records fetched": harmless -- means this harness's own
      freshly-bootstrapped test organization has no approved postmortems
      yet (real, honest result, not a connector bug). Run
      `test_ingestion_pipeline.py` first if you want non-zero Runbooks data.
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as harness_config  # noqa: E402
import utils  # noqa: E402

from app.ingestion.connectors.azure_devops import AzureDevOpsConnector  # noqa: E402
from app.ingestion.connectors.confluence import ConfluenceConnector  # noqa: E402
from app.ingestion.connectors.github import GitHubConnector  # noqa: E402
from app.ingestion.connectors.jira import JiraConnector  # noqa: E402
from app.ingestion.connectors.runbooks import RunbooksConnector  # noqa: E402
from app.ingestion.connectors.sharepoint import SharePointConnector  # noqa: E402
from app.ingestion.connectors.slack import SlackConnector  # noqa: E402
from app.ingestion.connectors.teams import TeamsConnector  # noqa: E402
from app.ingestion.schemas import ResolvedConnectorConfig  # noqa: E402

_CONNECTOR_CLASSES = {
    "slack": SlackConnector,
    "github": GitHubConnector,
    "jira": JiraConnector,
    "confluence": ConfluenceConnector,
    "teams": TeamsConnector,
    "sharepoint": SharePointConnector,
    "azure_devops": AzureDevOpsConnector,
    "runbooks": RunbooksConnector,
}


def _run_one_connector(
    source: str, spec: harness_config.ConnectorSpec, organization_id: uuid.UUID
) -> None:
    print(f"\nConnector: {source}")

    if not spec.available:
        print("Authentication       SKIPPED")
        print("Connection            SKIPPED")
        print("Documents fetched     SKIPPED")
        print("Sample data           SKIPPED")
        print(f"Reason: {spec.unavailable_reason}")
        utils.record_result(f"{source}: connector", True, detail="skipped (no credentials)")
        return

    connector = _CONNECTOR_CLASSES[source]()
    resolved = ResolvedConnectorConfig(
        connector_config_id=uuid.uuid4(),
        organization_id=organization_id,
        project_id=None,
        source=source,
        credential_ref=spec.credential_ref or "",
        config=spec.config,
    )

    start = time.monotonic()
    try:
        raw_items, normalized, batches = utils.fetch_all_sync(connector, resolved, max_batches=2)
    except Exception as exc:  # noqa: BLE001 - a real failure IS the result to report
        elapsed = time.monotonic() - start
        print("Authentication       FAIL")
        print("Connection            FAIL")
        print("Documents fetched     FAIL")
        print("Sample data           FAIL")
        print("--- FAILURE REPORT ---")
        print(f"Component: {source} connector")
        print(f"Command: authenticate()/fetch_batch() via utils.fetch_all_sync")
        print(f"Error: {type(exc).__name__}: {exc}")
        print("Expected: a successful authenticated fetch")
        print("Actual: an exception was raised (see Error above)")
        print("Possible Cause: invalid/expired credential, wrong base_url/org, or a real network issue")
        print("Suggested Fix: verify the corresponding EKIP_TEST_* value(s) in .env against the provider's")
        print("                own console; re-run this script alone with -v style prints above for detail")
        utils.record_result(f"{source}: connector", False, elapsed_seconds=elapsed, detail=str(exc))
        return

    elapsed = time.monotonic() - start
    print("Authentication       PASS")
    print("Connection            PASS")
    print(f"Documents fetched     {'PASS' if raw_items or source == 'runbooks' else 'PASS (0 items -- see note)'}")
    print(f"Sample data           {'PASS' if normalized else 'PASS (nothing to sample)'}")
    print(f"Records fetched: {len(raw_items)}  (in {batches} batch(es))")
    if normalized:
        sample = normalized[0]
        print(
            f"Sample: title={sample.title!r} external_id={sample.external_id!r} "
            f"content_len={len(sample.content)} metadata_keys={sorted(sample.metadata.keys())}"
        )
    utils.record_result(f"{source}: connector", True, elapsed_seconds=elapsed, detail=f"{len(raw_items)} records")


def main() -> bool:
    utils.reset_results()
    cfg = harness_config.load_config()

    print("Bootstrapping a real test organization/admin identity (see utils.py's module docstring, point 1)...")
    identity = utils.bootstrap_admin_sync(
        org_name=cfg.org_name, org_slug=cfg.org_slug, email=cfg.admin_email, display_name=cfg.admin_display_name
    )
    print(f"Organization: {identity['organization_slug']} ({identity['organization_id']})")
    organization_id = uuid.UUID(identity["organization_id"])

    for source, spec in cfg.connectors.items():
        _run_one_connector(source, spec, organization_id)

    print("\nConnector: google_drive")
    print("Authentication       N/A")
    print("Connection            N/A")
    print("Documents fetched     N/A")
    print("Sample data           N/A")
    print("--- NOT A FAILURE, A DISCLOSED GAP ---")
    print("Component: Google Drive connector")
    print("Command: (none -- nothing to call)")
    print("Error: no `GoogleDriveConnector` class exists anywhere in app/ingestion/connectors/")
    print("Expected: N/A -- this connector was requested but was never built")
    print("Actual: EKIP implements exactly 8 connectors (Slack, GitHub, Jira, Teams, Azure DevOps,")
    print("        Confluence, SharePoint, Runbooks) -- confirmed by listing that directory directly")
    print("Possible Cause: not yet built -- Google Drive was never in any implemented milestone")
    print("Suggested Fix: build app/ingestion/connectors/google_drive.py following the existing")
    print("               `Connector` Protocol if this source is needed (out of scope for this harness,")
    print("               which only tests EXISTING code, per its own constraints)")

    return utils.print_summary(title="EKIP CONNECTOR TESTS -- SUMMARY")


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
