# Deployment

## Current status

```
Local build/test/Docker:     VERIFIED — see the exact results in the Phase 3
                              Batch 4 completion report (backend tests,
                              import-linter, frontend checks, a real
                              migration run against a disposable database,
                              Dockerfile/compose syntax, Bicep compilation).
Azure infrastructure:        NOT PROVISIONED. BLOCKED — the identity available
                              during this work (mehul.agarwal@navikenz.com,
                              Navikenz "Dev subscription") has no
                              Contributor/Owner role anywhere on the
                              subscription; a real `az group create` attempt
                              was denied with AuthorizationFailed (Batch 3.5).
                              Re-confirmed with a precise finding during the
                              production-closure phase (2026-08-18): this
                              identity does hold Contributor on exactly one
                              existing resource group, `rg-nextcare-purview-
                              demo` — an unrelated pre-existing demo project,
                              not anything EKIP-designated. Explicitly
                              declined to deploy into it (would be using a
                              different project's resource group without
                              proper authorization scope, whatever the raw
                              API permissions allow) — Azure infrastructure
                              validation remains blocked on a real,
                              EKIP-designated resource group/subscription
                              access, not merely "any Contributor role
                              anywhere."
Azure Key Vault integration: Structurally implemented and unit-tested against
                              a mocked Key Vault boundary (7 tests). Never
                              exercised against a real vault -- same
                              permissions blocker.
Bicep template:               Recompiled clean (`az bicep build`, 0 errors/
                              warnings) during a Phase 8 infrastructure audit
                              (2026-08-18), which also found and fixed a real
                              bug the previous "syntax-validated" pass missed:
                              `openai-api-key` was stored in Key Vault but
                              never actually wired into either container app
                              (no `OPENAI_API_KEY` env var existed at all),
                              and `DATABASE_URL` was missing its password
                              entirely -- both `sharedEnvVars` entries
                              referenced a "secretRef... at deploy time" this
                              file never documented and the template never
                              implemented. Fixed via a real Container Apps
                              `secrets` block: `openai-api-key` now resolves
                              from Key Vault directly (needs a second,
                              separate "Key Vault Secrets User" role
                              assignment -- `Get` on secrets is a different
                              operation from the existing "Key Vault Crypto
                              User" `wrapKey`/`unwrapKey` grant, which stays
                              scoped to the KEK only), and `database-url` is
                              a Bicep-computed secret embedding the admin
                              password. The same audit found a second,
                              identical-shape bug: `REDIS_URL` was
                              `rediss://<host>:6380` with no access key at
                              all -- Azure Cache for Redis authenticates by
                              key, not by the deploying identity, so this
                              would have failed to connect outright. Fixed
                              the same way, via `redis.listKeys().
                              primaryKey` folded into a computed `redis-url`
                              secret. Still never deployed to a real
                              subscription -- all three fixes are
                              unexercised beyond the compiler itself, same
                              permissions blocker as above.
```

**Making this deployable does not mean it has been deployed.** Everything
below is ready to run the moment appropriate Azure permissions exist; none
of it has been applied to a real subscription.

## Target architecture

```
                    ┌─────────────┐
   Internet ───────▶│  Frontend   │  Container App (nginx, static build)
                    └──────┬──────┘
                           │ HTTPS
                    ┌──────▼──────┐
                    │   Backend   │  Container App (FastAPI, GET /health, /ready)
                    └──┬───────┬──┘
                       │       │
              ┌────────▼─┐   ┌▼──────────┐        ┌──────────────┐
              │PostgreSQL│   │   Redis    │◀──────▶│    Worker    │  Container App
              │ Flexible │   │  (Azure    │        │  (arq, same  │
              │  Server  │   │   Cache)   │        │  image, diff │
              └──────────┘   └────────────┘        │  command)    │
                                                     └──────┬───────┘
                                                            │
                                        ┌───────────────────▼──┐
                                        │   Azure Key Vault      │
                                        │ (connector/SSO secrets)│
                                        └────────────────────────┘
        Both Backend and Worker authenticate to Key Vault via one shared
        User-Assigned Managed Identity -- no Azure client secret is ever
        an application setting (infra/main.bicep, DefaultAzureCredential).
```

Defined in `infra/main.bicep` — resource group scope (the resource group
itself is assumed to already exist), no subscription ID/tenant ID/resource
group name hardcoded anywhere. Compiles cleanly (`az bicep build
infra/main.bicep`, verified in this environment, zero warnings).

## Procedure (once Azure access exists)

```
1. Build + test              scripts/deploy.sh build-and-test
2. Push images                docker push <registry>/ekip-backend:<tag>
                               docker push <registry>/ekip-frontend:<tag>
3. Provision infrastructure   az deployment group create \
                                 --resource-group <rg> \
                                 --template-file infra/main.bicep \
                                 --parameters infra/main.parameters.example.json \
                                 --parameters postgresAdminPassword=$PG_PW ekipAppPassword=$EKIP_APP_PW openAiApiKey=$OPENAI_KEY
4. Run migration               az containerapp job start --name <prefix>-migrate
                                 (one-shot Container Apps Job, infra/main.bicep's
                                 `migrateJob` — connects with the migration/admin
                                 credential to run `alembic upgrade head`, which
                                 includes provisioning/converging the `ekip_app`
                                 role to $EKIP_APP_PW; see "Database and Redis
                                 security" below for why this credential never
                                 reaches steps 5-6's containers)
5. Deploy backend              az containerapp update --image ...
6. Deploy worker               az containerapp update --image ...
7. Deploy frontend             az containerapp update --image ...
8. Health check                curl https://<backend>/health
9. Readiness check             curl https://<backend>/ready
```

`scripts/deploy.sh build-and-test` is real and runnable today (steps 1-2).
`scripts/deploy.sh azure-deploy` (steps 3-9) requires the permissions above.
`$EKIP_APP_PW` must be a **different** secret value than `$PG_PW` — they
authenticate two different roles with two different trust levels (see
below), not the same credential under two names.

## Migration database vs runtime database

Two distinct credentials, deliberately never interchangeable:

- **Migration/admin database** — `postgresAdminLogin`/`postgresAdminPassword`
  (Postgres's own server-admin account for the Flexible Server). Used
  **only** by `migrateJob`, which runs once per deploy, exits, and is never
  in the request path. Can `CREATE ROLE`, alter schema, and run every
  migration — including `b8f3d6a1c4e7`, which provisions the role below and
  deliberately cannot run as that role itself (`GRANT`/`CREATE ROLE` require
  admin privileges `ekip_app` is never given).
- **Runtime/application database** — `ekip_app` (`ekipAppPassword`),
  `NOSUPERUSER`/`NOBYPASSRLS`. Used by `backendApp`/`workerApp` for the
  entire lifetime of the deployment — ordinary DML only, every RLS policy
  from `c7d4e8f19a2b` actually enforced against every query, unlike every
  environment observed before this fix (`neondb_owner`, confirmed
  `bypassrls=true`).

The admin credential is never present in `runtimeSecrets` (`infra/main.
bicep`) and therefore never reaches `backendApp`/`workerApp`/`frontendApp` —
`migrateJob` is the one place in the whole template it's used at all. This
is the direct fix for `EKIP_TENANT_ISOLATION_SECURITY_REVIEW.md`
recommendation #2's finding that every environment observed so far put one
single, superuser-equivalent credential in both places.

## Azure identity and Key Vault access

`infra/main.bicep` provisions one **user-assigned managed identity**,
shared by the backend and worker container apps (they run identical code),
granted exactly two built-in roles scoped to the one Key Vault, each for a
distinct operation on a distinct object type: **Key Vault Crypto User**
(`WrapKey`/`UnwrapKey` on `connectorSecretsKey`, the KEK the app's own KMS
abstraction uses for envelope-encrypted connector/SSO credentials) and
**Key Vault Secrets User** (`Get` on `openAiSecret`, which Container Apps'
own `keyVaultUrl` secret reference reads at container-start time) — never
`Owner`, never `Contributor`, never `Manage Access Policies`. Authentication uses
`DefaultAzureCredential`, which resolves to the container app's managed
identity automatically in production and to a developer's own `az login`
session locally — no Azure client secret is ever stored as an application
setting anywhere.

## Database and Redis security (target configuration)

**PostgreSQL** (`infra/main.bicep`'s `postgres` resource):
- TLS enforced by Azure Database for PostgreSQL Flexible Server by default.
- `publicNetworkAccessEnabled` parameterized, defaulting to `false` — a real
  production deployment should reach this only via VNet integration/private
  endpoint (not yet defined in this template — flagged as a follow-up, not
  silently assumed done).
- 7-day backup retention configured; geo-redundant backup left off by
  default (parameterizable) pending an explicit RPO/RTO decision.
- **Backup/restore live test: BLOCKED.** Azure automated backups are a
  configured setting on a resource that has never been provisioned (same
  permissions blocker throughout this file) — no actual backup has ever
  been taken, and no restore has ever been exercised, automated or manual.
  Treat "backup retention configured" as a template setting, not a proven
  recovery capability, until a real Flexible Server exists to test
  point-in-time restore against.
- Connection pooling: the application already uses SQLAlchemy's async
  engine with its own pool (`app/database/session.py`); PgBouncer/equivalent
  in front of the server is a further hardening step, not yet provisioned.
- Least-privilege application credential: **done** — `backendApp`/
  `workerApp` connect as `ekip_app` (`NOSUPERUSER`/`NOBYPASSRLS`), not the
  server admin account; see "Migration database vs runtime database" above.
  Still unexercised against a real deployment, same permissions blocker as
  everything else in this file.

**Redis** (`infra/main.bicep`'s `redis` resource):
- TLS-only (`enableNonSslPort: false`, `minimumTlsVersion: '1.2'`).
- Standard (not Basic) tier for a real SLA.
- The intermittent Redis Cloud instability observed in Phase 2/3 is a
  property of that specific third-party dev instance, not of this
  application's Redis handling — `app.api.main._lifespan` and the arq
  worker already tolerate a Redis outage without crashing the whole API
  (see `docs/operations/local-production.md` and the health-check design
  in `app/api/routers/health.py`). Moving to Azure Cache for Redis (a
  managed, SLA-backed service) removes the specific instability observed,
  but the application-level resilience already exists independent of that.

## Release strategy

```
development (local/.env)
      ↓
CI (.github/workflows/ci.yml — every PR)
      ↓
staging (not yet provisioned — same Bicep template, different
         namePrefix/environmentTag, its own resource group)
      ↓
production (not yet provisioned)
```

No environment currently deploys automatically from a developer machine;
`scripts/deploy.sh azure-deploy` is meant to run from CI (or a controlled
operator session) against a specific, named resource group — never as an
implicit side effect of merging to `main`.

## Rollback

See `docs/operations/rollback.md`.
