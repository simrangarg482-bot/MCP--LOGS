# Security incident record

A running log of credential/secret exposure incidents and their resolution.
Never records an actual secret value — type, location, and timeline only.

## 2026-08-18 — `.env` values surfaced in agent tool output

**Credential type:** Azure DevOps personal access token
(`EKIP_TEST_AZURE_DEVOPS_PAT`), the Neon PostgreSQL connection string's
embedded password (`DATABASE_URL`), and the Redis Cloud connection string's
embedded password (`REDIS_URL`).

**Exposure location:** A coding-assistant session's tool-call output, while
auditing `.env` for non-secret configuration ahead of a database-migration
investigation. A `grep -v` filter intended to exclude lines matching
`KEY|SECRET|PASSWORD|TOKEN` did not match the `EKIP_TEST_AZURE_DEVOPS_PAT`
line (contains `PAT`, not `TOKEN`) or the `DATABASE_URL`/`REDIS_URL` lines
(the variable names themselves don't match any of those substrings, even
though the values contain embedded credentials) — those three lines passed
the filter unredacted.

**Exposure window:** A single tool-output print, within one working session.
Not published, not committed, not sent to any third-party service beyond the
coding assistant's own model context for that session.

**Whether committed to Git:** **No.** Confirmed structurally (not by
searching for the value itself):
- `.env` is listed in `.gitignore` (`.env`, `.env.*`, with explicit
  exceptions only for `.env.example`/`.env.docker.example`).
- `git log --all -- .env` returns no history — `.env` has never been
  committed on any branch, ever.
- `git ls-files | grep -iE '\.env'` shows only `.example` variants tracked
  (`frontend/.env.example`, `scripts/realworld_onboarding/.env.example`,
  `tests/ingestion_retrieval/.env.example`) — no real secret-bearing file is
  or has ever been tracked.

Because the credentials were never in git history, no history rewrite
(`git filter-repo`/BFG) is needed or was performed — see section 15's
"do not rewrite shared Git history automatically" instruction. The
recommended remediation is rotation only, per the standard "any credential
that left its intended boundary is treated as compromised" rule regardless
of whether it reached git.

**Whether rotated:** **NOT YET — requires action outside this environment's
tool access.** Rotating the Azure DevOps PAT requires the Azure DevOps
organization's own token-management UI; rotating the Neon database password
requires the Neon console; rotating the Redis Cloud password requires the
Redis Cloud console. None of these are reachable from this coding session —
this is a explicit stop condition (item 18: "rotation permissions are
unavailable ... report the blocker rather than improvising").

**Replacement mechanism (once rotated by an operator with console access):**
1. Azure DevOps PAT — scope the replacement to the minimum needed (read-only
   access to the specific projects `EKIP_TEST_AZURE_DEVOPS_PROJECTS`
   lists), set the shortest practical expiry, and store it only in `.env`
   (already gitignored) or the CI secret store — never in a committed file.
   Prefer a service connection / workload identity over a personal PAT if
   this integration ever runs from CI rather than a developer's own `.env`.
2. Neon database password — rotate via the Neon console's own credential
   rotation, update `DATABASE_URL` in `.env` (local) and whichever secret
   store backs any deployed environment; verify `uv run alembic current`
   and application startup succeed against the new credential before
   discarding the old one.
3. Redis Cloud password — same pattern via the Redis Cloud console; verify
   `app.api.main._lifespan`'s pool creation and the arq worker's own
   connection both succeed post-rotation (the worker connects independently
   of the API process — both need the new value).

**Preventive measure added:** `secret-scan` job in
`.github/workflows/ci.yml` (gitleaks, `.gitleaks.toml`) — runs on every PR
and push to `main`, scans full git history, includes a project-specific rule
for connection strings with an inline `user:password@host` (the exact shape
of this incident), and redacts any match's value in its own output.

**Post-rotation verification checklist (all three credentials, every
environment they're configured in — local `.env`, CI secrets, any deployed
environment's secret store):**
1. Update the deployment configuration everywhere the old value is
   referenced — `.env` (local), CI repository secrets, and (once a real
   Azure deployment exists) the Key Vault secret / Container Apps
   `secretRef` backing it. Miss one and that environment silently keeps
   using the old, compromised value even after the console-side rotation.
2. Verify the application actually authenticates with the new value —
   `uv run alembic current` for the database credential, application
   startup (`/health` and `/ready` both green) for all three, before
   discarding the old credential anywhere.
3. Run the secret scan (`gitleaks detect --source . --config .gitleaks.toml
   --redact -v`, or let the CI job do it on the next push) to confirm the
   *new* value was never accidentally committed in the process of updating
   configuration files.
4. Grep the repository and any exported CI logs for the old credential's
   distinguishing prefix (never the value itself) to confirm nothing still
   references it once the console-side rotation revokes it — a leftover
   reference to an already-revoked credential is a broken-not-insecure
   failure mode, but still worth catching before it causes a confusing
   outage.

Separately, and not part of rotating *this* incident's three credentials:
once `ekip_app` (migration `b8f3d6a1c4e7`) is actually wired into a real
environment's `DATABASE_URL`, its password becomes a fourth credential this
same checklist applies to going forward — distinct from, and rotated
independently of, the Postgres admin/migration password.

## Format for future entries

```
## YYYY-MM-DD — short title

Credential type:
Exposure location:
Exposure window:
Whether committed to Git:
Whether rotated:
Replacement mechanism:
Preventive measure added (if any):
```
