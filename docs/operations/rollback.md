# Rollback

What to do when a deployment goes bad. Read `docs/operations/deployment.md`
first for the normal forward path this is the inverse of.

## Application rollback (backend / worker / frontend)

Every deployed image is tagged with the short git SHA it was built from
(`scripts/deploy.sh`'s `IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD)}"`),
and pushed to the registry before any container app is updated. Rolling back
an application (not a database) release is therefore just re-pointing the
container app at the previous known-good tag — no rebuild required:

```bash
az containerapp update --name <name-prefix>-backend  --resource-group <rg> --image <registry>/ekip-backend:<previous-sha>
az containerapp update --name <name-prefix>-worker   --resource-group <rg> --image <registry>/ekip-backend:<previous-sha>
az containerapp update --name <name-prefix>-frontend --resource-group <rg> --image <registry>/ekip-frontend:<previous-sha>
```

Backend and worker always move together — they run the exact same image
(`Dockerfile`'s own comment: one image, different `command:`), so rolling
back one without the other risks a schema/code mismatch between the API and
the jobs it enqueues.

Container Apps keeps prior revisions by default; `az containerapp revision
list` / `az containerapp ingress traffic set` can shift traffic back to an
already-running previous revision even faster than a fresh `update`, if that
revision hasn't been deactivated.

After any rollback: re-run the health/readiness check from
`scripts/deploy.sh`'s own final step (`curl .../health` and `.../ready`)
before considering the rollback complete.

## Database migration rollback

**Do not assume this is safe by default.** Alembic's `downgrade` machinery
exists and every migration in `app/database/migrations/versions/` defines a
`downgrade()`, but this repo has never exercised a real downgrade against
data created by the corresponding upgrade — only forward (`upgrade head`)
has been validated (empty-DB and upgrade-from-previous, per
`docs/operations/ci.md`'s `migration-validation` job). Treat downgrade as
**structurally present, not proven safe**, especially for any migration that
drops a column/table or narrows a constraint — those are destructive in the
downgrade direction even when the upgrade was purely additive.

Decision rule:

- **Additive migration** (new nullable column, new table, new index) whose
  application code rollback (above) simply stops reading/writing the new
  structure: prefer **leaving the schema as-is** and rolling back the
  application only. An unused column/table is harmless; a destructive
  `downgrade()` run against a table that already has real rows is not
  something to reach for reflexively.
- **Migration that must be reversed** (e.g. a bad constraint actively
  rejecting valid writes): run `alembic downgrade -1` against a fresh backup
  first, in a non-production environment, and confirm the specific
  migration's `downgrade()` does what you expect before running it against
  real data.
- **Migration that already changed/removed data** (backfills, column drops):
  prefer a **forward-fix migration** (a new migration that re-adds/repairs
  the affected structure) over `downgrade`, unless the specific migration
  was verified reversible ahead of time. This matches section 30's
  instruction not to claim rollback is safe unless it's actually reversible.

Always take a database backup/snapshot immediately before running any
production migration (`upgrade` or `downgrade`) — Azure Database for
PostgreSQL Flexible Server's automated backups (`infra/main.bicep`'s
`backup.backupRetentionDays: 7`) are the safety net if a migration goes
wrong in a way `downgrade` can't cleanly undo.

## Worker rollback

Covered by "Application rollback" above — the worker is the same image as
the backend, rolled back the same way. No separate job-queue state to
migrate: arq jobs are transient (Redis-backed), not versioned against a
specific worker release, so rolling the worker image back doesn't require
draining or replaying in-flight jobs beyond arq's own existing retry
semantics (a job that was mid-flight during the bad deploy either completes
under the old code after rollback or is retried per its own configured
`max_tries`).

## Frontend rollback

Covered by "Application rollback" above. One frontend-specific caveat: the
running frontend's bundle already has `VITE_API_BASE_URL` compiled in from
build time (`frontend/Dockerfile`'s own comment on why this can never be a
runtime env var) — rolling back to a previous frontend image is only safe
if that previous image was built against a backend origin that's still
valid today. If the backend's public URL itself changed since that image
was built, rebuild from the previous frontend source commit with the
current `VITE_API_BASE_URL` instead of reusing the old image verbatim.

## When rollback isn't the right call

A rollback undoes a bad *deployment*. It does not undo bad *data* written by
the bad deployment while it was live (e.g., a code bug that wrote incorrect
rows). If the failed release was live long enough to write data, plan a
separate data-remediation step — rollback alone does not fix already-written
rows.

## Current status

Everything above is a documented procedure, not something exercised against
a real Azure deployment — no application release has ever been deployed to
Azure to roll back (see `docs/operations/deployment.md`'s "Current status").
The `az containerapp update`/`revision`/migration commands here are correct
for the resources `infra/main.bicep` defines, but are unverified against a
real subscription for the same permissions reason documented throughout
Batch 4.
