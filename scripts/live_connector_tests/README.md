# EKIP Live Connector Integration Tests

Real, pytest-shaped integration tests for all 8 of EKIP's ingestion
connectors — one file per connector, each making genuine network calls (or,
for Runbooks, a genuine database read) against real external services using
real credentials. **No existing application, connector, or unit-test file
was modified to build these** — everything here is new and additive.

## Why these live under `scripts/`, not `tests/`

`pyproject.toml` sets `testpaths = ["tests"]`, so a bare `pytest` (what CI or
a normal full-suite run types) auto-collects everything under `tests/`.
These tests make **real** network calls with **real** credentials — letting
them be auto-collected would mean an ordinary `pytest` run could hang, fail,
or make unwanted live API calls on any machine, regardless of whether it has
connector credentials configured. This project already made this exact call
once before: `scripts/test_connectors.py`'s own docstring states it lives in
`scripts/`, not `tests/`, for this same reason. These files follow that
established precedent — they are real pytest tests (proper fixtures,
PASS/FAIL reporting, `-k`/`-v` filtering all work normally), just never
auto-run by accident. Run them explicitly:

```bash
pytest scripts/live_connector_tests/ -v -s
# or one connector at a time:
pytest scripts/live_connector_tests/test_github_live.py -v -s
```

They do **not** replace or touch the existing, fully-mocked unit tests at
`tests/ingestion/connectors/test_*.py` — those still run as part of the
normal `pytest` suite, unmodified, and still verify `normalize()`/parsing
logic without any network dependency. This new suite verifies the parts
those mocked tests structurally cannot: that `authenticate()` and
`fetch_batch()` actually work against the real APIs.

## Credentials — reused, not duplicated

These tests load credentials from **`tests/ingestion_retrieval/.env`**
(via that directory's `config.py`) — the same file already used by
`tests/ingestion_retrieval/test_connectors.py`. If you've already filled
that in, you don't need to do anything else. Any connector left blank there
is **SKIPPED** here too (`pytest.skip`, printed clearly), never silently
treated as a failure.

If you haven't set it up yet: `cp tests/ingestion_retrieval/.env.example
tests/ingestion_retrieval/.env` and fill in whichever connectors you have
credentials for.

A real `DATABASE_URL` must also be set in the **project's own root** `.env`
(needed to bootstrap a real test organization, and for the Runbooks
connector specifically).

## What each file verifies, and what it requires

| File | Verifies | Requires (in `tests/ingestion_retrieval/.env`) |
|---|---|---|
| `test_slack_live.py` | Real `auth.test`; real message fetch + normalize; Slack's own `has_more`/`next_cursor` pagination; real `oldest`-param `since` filtering | `EKIP_TEST_SLACK_BOT_TOKEN`, `EKIP_TEST_SLACK_CHANNEL_IDS` |
| `test_github_live.py` | Real `/rate_limit` auth check; real file tree fetch + normalize; pagination across this connector's 4 internal phases (files/commits/pulls/issues); real `since=` filtering on the commits phase specifically | `EKIP_TEST_GITHUB_TOKEN`, `EKIP_TEST_GITHUB_REPO`, `EKIP_TEST_GITHUB_REF` (optional) |
| `test_jira_live.py` | Real `/myself` auth check; real JQL issue search + normalize; pagination via Jira's own `total` count; real JQL `updated >=` filtering | `EKIP_TEST_JIRA_BASE_URL`, `EKIP_TEST_JIRA_EMAIL`, `EKIP_TEST_JIRA_API_TOKEN`, `EKIP_TEST_JIRA_PROJECTS` |
| `test_confluence_live.py` | Real `/space` auth check; real CQL content search + normalize; pagination via a page-length heuristic; real CQL `lastmodified >=` filtering | `EKIP_TEST_CONFLUENCE_BASE_URL`, `EKIP_TEST_CONFLUENCE_EMAIL`, `EKIP_TEST_CONFLUENCE_API_TOKEN`, `EKIP_TEST_CONFLUENCE_SPACES` |
| `test_teams_live.py` | Real Graph `/me` auth check; real full-sync message fetch + normalize; Graph `@odata.nextLink` pagination; real Graph delta query (`$filter=lastModifiedDateTime gt ...`) | `EKIP_TEST_TEAMS_ACCESS_TOKEN` (already-issued Graph token, expires ~1hr), `EKIP_TEST_TEAMS_TEAM_ID`, `EKIP_TEST_TEAMS_CHANNEL_IDS` |
| `test_sharepoint_live.py` | Real Graph `/me` auth check; real file fetch + normalize (pre-filtered to supported extensions); Graph `@odata.nextLink` pagination; client-side `since` filtering | `EKIP_TEST_SHAREPOINT_ACCESS_TOKEN` (already-issued Graph token, expires ~1hr), `EKIP_TEST_SHAREPOINT_SITE_IDS` |
| `test_azure_devops_live.py` | Real `_apis/projects` auth check; real WIQL work-item query + normalize; pagination via WIQL result-list length; real WIQL `[System.ChangedDate] >=` filtering | `EKIP_TEST_AZURE_DEVOPS_ORG`, `EKIP_TEST_AZURE_DEVOPS_PAT`, `EKIP_TEST_AZURE_DEVOPS_PROJECTS` |
| `test_runbooks_live.py` | No network call (internal connector) — real DB read of the bootstrapped test org's postmortems + normalize; page-length-heuristic pagination; `since` pass-through | None (needs project's own `DATABASE_URL` only) |

Every file also has a docstring at the top with the exact same detail.

## Reading the results

Every test prints a `PASS: ...` line on success describing exactly what
real call it just made. A `SKIPPED` result (not a failure) means either:
credentials weren't configured for that connector, or the real data
returned happened not to exercise a particular path (e.g. a channel with
fewer messages than one page — pagination genuinely wasn't triggered by
real data, which is itself a legitimate, correct outcome, not a bug).

## Known, disclosed limitations (not fixed here — these are real product
observations from writing these tests, reported per this task's own
instructions rather than silently worked around)

1. **Azure DevOps invalid-PAT detection is incomplete.** Azure DevOps
   sometimes returns HTTP 203 with an HTML sign-in redirect for a bad PAT
   instead of 401/403. `authenticate()`'s `raise_for_status()` only raises
   on status >= 400, so a 203 response would slip through undetected. This
   suite's negative test only exercises the case that IS caught (an
   outright-rejected credential); it does not reproduce the 203 case, since
   that depends on Azure DevOps' own behavior and isn't reliably forceable
   from a test.
2. **`teams.py` line 279's docstring reference to a `_is_recent_enough`
   method is stale** — no such method exists in `teams.py` (it only exists
   in `sharepoint.py`). Looks like a documentation leftover from an earlier
   version; the current code's delta-query approach needs no client-side
   re-filter, per that same module's own docstring elsewhere.
3. **Runbooks' `since` filtering is not independently re-verified here** —
   `runbooks.py` passes `since` straight through to
   `core.incidents.service.list_postmortems_for_ingestion`, which is outside
   `app/ingestion/connectors/` and wasn't re-read as part of this task.
