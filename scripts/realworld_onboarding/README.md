# EKIP Real-World Onboarding & SSO Test Harness

Standalone test scripts that exercise EKIP's real, running REST API the way
an actual customer's onboarding engineer would, after purchasing EKIP:
register an organization, configure SSO, create users, log in, verify
tokens, and probe permissions/isolation/negative cases/logout.

**Nothing under `app/` is modified, renamed, refactored, or fixed by
anything in this directory.** Every file here is new, lives entirely under
`scripts/realworld_onboarding/`, and treats the rest of the project as
read-only. Where a script needs project code that has no REST/MCP
equivalent (see "What this exercise found," below), it imports the
project's own, unmodified functions directly -- the same pattern
`scripts/seed_test_organization.py` already established -- and says so
explicitly in its own docstring.

## Could this actually be run and verified in this environment?

**No, and that needs to be said plainly.** The sandbox this harness was
built in had no working shell all session (`Workspace unavailable...Not
enough disk space to set up the workspace`) -- there was no way to start
Postgres/Redis, run `python scripts/run_api_server.py`, or execute a single
one of these scripts to confirm they work end-to-end. Every script below
was written by reading the actual, current implementation directly (routers,
services, schemas, exceptions, migrations) and is believed correct against
that reading, but **none of it has been executed.** Run it yourself, in
your own environment, and treat the first real run as this harness's real
test.

## Prerequisites

1. **The project's own dependencies**, installed and importable:
   `pip install -r requirements.txt` at the repo root (this harness's own
   `common/bootstrap.py` and `common/jwt_tools.py` import `app.*` directly).
2. **This harness's own, smaller dependency set**:
   `pip install -r scripts/realworld_onboarding/requirements.txt`
3. **A reachable Postgres** matching the project's own `.env` `DATABASE_URL`
   (run `python scripts/diagnose_db_connection.py` from the repo root to
   check this first).
4. **The EKIP API server running**, reachable at whatever `BASE_URL` you
   put in this harness's `.env` (see below):
   ```
   python scripts/run_api_server.py
   ```
   This starts `uvicorn` on `http://0.0.0.0:8000` by default.
5. **This harness's own `.env`**, separate from the project's:
   ```
   cp scripts/realworld_onboarding/.env.example scripts/realworld_onboarding/.env
   ```
   Every variable is documented inline in `.env.example`. You do **not**
   need real Identity Provider credentials to run Scripts 01-04, 06-10 --
   only Script 05 (a genuine SSO login) needs them.

## What this exercise found, before writing a single test

Reading the real implementation before automating anything (as the task
required) surfaced five things worth knowing before you run this harness,
none of which this harness works around by inventing new behavior --
each is disclosed at the point in the flow where it matters:

1. **There is no self-service organization signup.** `POST /organizations`
   requires an authenticated `CurrentIdentity` (`app/api/deps.py`) -- there
   is no anonymous, public registration endpoint anywhere in EKIP. A
   genuinely new customer, with zero prior EKIP identity, cannot call this
   endpoint cold.
2. **Organization creation grants no role to anyone.** Even once an
   organization exists, `core.tenancy.service.create_organization` performs
   no permission check (confirmed in its own docstring: "no permission
   check is added here, since one still isn't specified") and assigns no
   role to the creator. Immediately after creation, nobody has
   `tenancy:manage` in the new organization -- every other tenancy-admin
   endpoint (SSO config, invitations, access rules) is unreachable until
   someone is given a role.
3. **There is no API to create a Role, create a Permission, or grant a
   Permission to a Role**, anywhere in this codebase (confirmed by reading
   every router under `app/api/routers/` and all of
   `app/core/users/service.py`). The only existing precedent for solving
   this is `scripts/seed_test_organization.py`'s direct ORM access for one
   hardcoded local-dev organization.
4. **`POST /invitations/{id}/accept` does not create a user or assign a
   role.** Reading `core.tenancy.service.accept_invitation` directly shows
   it only flips the invitation's `status` -- provisioning
   (`_resolve_or_provision_user`) only happens as a side effect of a real
   SSO login completing.
5. **A predicted (not yet empirically confirmed) RLS bug in organization
   creation itself.** `core.tenancy.service.create_organization` inserts
   the organization's mandatory default "General" `projects` row
   *without ever calling `set_tenant_context` first*. Reading the actual
   RLS migration (`c7d4e8f19a2b_milestone_10_row_level_security.py`) shows
   `projects` is one of the tables directly protected by the standard
   `tenant_isolation` policy (`USING (organization_id = current_setting(
   'app.current_organization_id', true)::uuid)`, `FOR ALL`, `FORCE ROW
   LEVEL SECURITY`) -- and Postgres uses that same `USING` expression as
   the implicit `WITH CHECK` for INSERT when none is given. With no GUC
   set, `current_setting(..., true)` returns `NULL`, and
   `organization_id = NULL` is never true -- so on a database where RLS is
   actually enforced (i.e. the application's own DB role is *not* a
   superuser/bypass role, which is the security review's own recommended
   configuration), **inserting that default project should fail with a
   Postgres row-security-policy violation, and `01_register_org.py`/
   `common.bootstrap.get_or_create_organization` would fail with it.**
   This is a prediction from reading the code, not a confirmed bug --
   there was no live Postgres available to actually trigger it in this
   session. **If you hit an `asyncpg`/`psycopg` "new row violates row-level
   security policy for table \"projects\"" error while running Script 01
   or Script 02, this is why**, and it is a real product bug to report,
   not a problem with this harness. If your database role happens to
   bypass RLS (e.g. it owns the tables and `FORCE ROW LEVEL SECURITY`'s
   known superuser exception applies), you will not see this at all --
   which is itself the second half of the same finding: this bug's
   visibility depends entirely on which DB role is deployed, exactly the
   configuration risk the project's own tenant-isolation security review
   already flagged as its top open item.

**Update from a real run:** Finding 5 above did NOT manifest on the first
real execution against a live (Neon) Postgres -- `common.bootstrap`'s
`create_organization` call, including the auto-created default `projects`
row, committed cleanly. Either the deployed DB role isn't strictly
RLS-enforced in this environment (plausible -- it's the same class of risk
the tenant-isolation security review's own top recommendation already
names), or this specific Postgres-policy-semantics prediction was wrong.
Left uncorrected-but-struck-through above rather than deleted, since the
reasoning that produced it is still worth knowing -- treat it as a "watch
for this" note rather than a confirmed defect now.

None of the above are fixed here -- per the task's explicit constraint,
this harness works *around* each one (documented at the top of the
relevant script) and reports them; it does not patch `app/`.

## Running the whole flow

```
python scripts/realworld_onboarding/99_master_e2e.py
```

Runs Scripts 01 through 10 in order, stopping at the first failure (pass
`--continue-on-failure` to run everything regardless), then attempts
best-effort cleanup, then prints one combined PASS/FAIL summary table.

Or run any stage individually, in order (each writes to a shared
`.state.json` the next one reads from):

| # | Script | What it does | Calls a real API? |
|---|--------|---------------|--------------------|
| 01 | `01_register_org.py` | Register the organization | Yes (`POST /organizations`) |
| 02 | `02_bootstrap_org_admin.py` | Create the org's first administrator | No -- see script docstring (Finding 1-3) |
| 03 | `03_configure_sso.py` | Configure SSO | Yes (`POST .../sso/configure`) |
| 04 | `04_create_test_users.py` | Invite + seed 4 more personas | Part 1 yes, Part 2 no -- see script docstring (Finding 4) |
| 05 | `05_login_flow.py` | Real Authorization Code + PKCE login | Yes, if `.env` has real IdP creds; otherwise a documented skip |
| 06 | `06_verify_token.py` | Decode + verify a JWT | Yes (`GET /auth/me`, cross-check) |
| 07 | `07_permission_tests.py` | Allow/deny matrix across 5 roles x 4 endpoints | Yes |
| 08 | `08_isolation_tests.py` | Org A cannot see Org B's data, and vice versa | Yes |
| 09 | `09_negative_tests.py` | 11 automated negative cases + 5 documented IdP-only skips | Yes (11), N/A (5) |
| 10 | `10_logout_tests.py` | Refresh rotation, self/admin logout-all, re-login | Yes |
| -- | `cleanup.py` | Best-effort session/invitation/access-rule cleanup | Yes (partial -- see its own docstring) |

Every script is also independently runnable and documents itself in full
(purpose, APIs called, expected input/output, exact run command, common
failures, troubleshooting, and expected success output) in its own module
docstring -- read the script itself for the authoritative, most detailed
version of everything summarized in this README.

## Shared utilities (`common/`)

- `config.py` -- loads this harness's own `.env` (never the project's).
- `logger.py` -- `StepLogger` (per-stage request/response/timing logging)
  and the final colored PASS/FAIL summary table.
- `http.py` -- a thin, logging `httpx` wrapper. Talks to the real, running
  API over the network -- never `app.api`'s in-process `TestClient`.
- `state.py` -- a local `.state.json` scratch file handing IDs/tokens from
  one script to the next.
- `jwt_tools.py` -- unverified JWT decoding, plus verification via the
  project's own real `core.auth.service.verify_access_token`.
- `bootstrap.py` -- **the one module that doesn't call the REST API.**
  Read its module docstring in full before assuming any script that
  imports it is "cheating" -- it exists specifically because of Findings
  1-4 above, and documents exactly why, referencing the same precedent
  already set by `scripts/seed_test_organization.py`.

## Personas used throughout this harness

Defined once, in `common/bootstrap.py`, reused by every script:

| Persona | Permissions granted | Used to prove |
|---|---|---|
| `admin` | all 6 | full access; the only one who can configure SSO/invitations/access-rules |
| `security_engineer` | `incident:write`, `postmortem:write`, `observability:read` | a realistic elevated-but-not-admin role |
| `developer` | `incident:write` | narrowest write access |
| `manager` | `postmortem:approve`, `knowledge:review`, `observability:read` | review/approval without incident authorship |
| `read_only` | `observability:read` | read-only baseline; should be denied everywhere else |

(EKIP's complete, real permission catalog -- confirmed by grepping every
`require_permission(actor, "...")` / `_..._PERMISSION = "..."` call site in
`app/` -- is exactly six codes: `tenancy:manage`, `incident:write`,
`postmortem:write`, `postmortem:approve`, `knowledge:review`,
`observability:read`. There is no `incident:read` or similar read-gate --
reading incidents/postmortems is not permission-gated at all today.)

## Troubleshooting

- **`ConnectionRefused` / "Could not reach ..."**: the API server isn't
  running at `BASE_URL`. Start it: `python scripts/run_api_server.py`.
- **`httpx.ReadTimeout` on the very first REST call of a run** (this
  harness's own DB writes via `common.bootstrap` succeed and commit fine
  right before it): the API server process is very likely still alive but
  hasn't finished establishing its own database connection pool yet (a
  cold Neon compute waking up -- the same delay
  `scripts/diagnose_db_connection.py`'s own docstring describes; this
  harness's DB writes use a separate, already-warm connection, which is
  why they don't show the same delay). Check the API server's own terminal
  for what it's doing; `REQUEST_TIMEOUT_SECONDS` in `.env` defaults to 45s
  for exactly this reason -- raise it further (e.g. to 90) if your Neon
  compute is slow to resume, or simply hit `http://localhost:8000/docs` in
  a browser once first to "warm" the server before running this harness.
- **`TokenVerificationUnavailable` from `06_verify_token.py` or
  `09_negative_tests.py`**: run this harness's scripts with the *project's*
  virtualenv active, not a fresh/isolated one containing only this
  directory's own `requirements.txt`.
- **Local token verification fails for a token the live server accepts
  fine**: your shell's `JWT_SECRET_KEY` (from the *project's* `.env`,
  loaded via `app.shared.config.settings`) doesn't match what the running
  API server loaded. Run everything from the same shell/environment you
  started the server from.
- **`asyncpg`/`psycopg` row-level-security policy violation on `projects`
  during Script 01/02**: see Finding 5 above -- likely a real product bug,
  not a harness bug.
- **422 validation errors resolving `grants_role`**: run
  `02_bootstrap_org_admin.py` before `03_configure_sso.py`/
  `04_create_test_users.py` -- it creates the role catalog every later
  script assumes exists.
- **`SSO_PROVIDER` validation error**: must be exactly one of
  `entra_id | okta | auth0 | google_workspace` (EKIP adds no others; see
  `app/core/tenancy/schemas.py`'s `SSOProvider` literal).
- **`RuntimeError: Event loop is closed` from `common/bootstrap.py`,
  usually intermittent (some persona-seeding calls in the same run succeed,
  others fail)**: this was a real bug in this harness itself, now fixed --
  `bootstrap_persona_sync`/`set_user_active_sync` used to call
  `asyncio.run(...)` fresh on every invocation, but `app.database.session`'s
  async engine is a module-level singleton whose pooled `asyncpg`
  connections stay bound to whichever event loop first opened them; calling
  `asyncio.run()` more than once in one process (e.g. `04_create_test_users.py`
  seeding four personas in a loop, or `99_master_e2e.py` running every stage
  in one process) eventually reused/closed a connection tied to an
  already-closed loop. Fixed by having every call in `common/bootstrap.py`
  share one persistent event loop for the life of the process instead. If
  you still see this after pulling the latest version of this harness,
  it's a new instance of the same class of bug -- check for any remaining
  `asyncio.run(...)` call in `common/bootstrap.py` (there should be none;
  everything routes through the shared `_run()` helper).

## What a REAL SSO login additionally requires (Script 05)

This harness cannot fabricate a real Identity Provider. To exercise
`05_login_flow.py` for real, you need, at one of EKIP's four already-
supported providers:

1. A registered OIDC application (Authorization Code + PKCE), with its
   `REDIRECT_URI` registered *exactly* as it appears in this harness's
   `.env` (OAuth2 requires byte-for-byte equality).
2. That application's `client_id` / `client_secret` / issuer URL, in this
   harness's `.env`.
3. A real test user at that IdP able to complete an interactive login
   (`TEST_EMAIL` is informational only -- this harness does not attempt to
   automate typing a password into the IdP's own login page; you complete
   that step in a real browser and paste the resulting redirect URL back
   to the script, as its prompt explains).

Without this, Script 05 prints a clear, documented skip and every other
script continues to work using real, normally-signed sessions seeded via
`common/bootstrap.py` in place of a live login.
