# Deployment: Railway (full stack)

A single-provider alternative to `docs/operations/deployment.md`'s Azure
target and `deployment-cloudflare.md`'s split. Railway runs **everything** —
Postgres, Redis, and all four application processes plus the frontend — as
services in one project, wired together over Railway's private network.

Unlike the other two paths this one is **not** blocked on external access:
it needs only a Railway account and this repo. Nothing is deployed by the
repo itself — the steps below are all dashboard actions.

## Target architecture

```
                    Internet
              ┌────────┬──────────┬─────────────┐
              │        │          │             │
      ┌───────▼──┐ ┌───▼─────┐ ┌──▼──────────┐  │  (public domains)
      │ frontend │ │ backend │ │ mcp         │  │
      │ nginx    │ │ uvicorn │ │ streamable- │  │
      │ :$PORT   │ │ :$PORT  │ │ http :$PORT │  │
      └────┬─────┘ └────┬────┘ └──────┬──────┘  │
           └── API ─────┤             │         │
                        │   ┌─────────┴───┐     │
                        │   │ ingestion    │    │  (no domain)
                        │   │ worker       │    │
                        │   ├─────────────┐│    │
                        │   │ agents       ││   │  (no domain)
                        │   │ worker       ││   │
                        │   └──────┬───────┘│   │
             private network      │        │   │
              ┌──────────┬────────┴────────┴───┘
        ┌─────▼─────┐ ┌──▼─────┐
        │ Postgres  │ │ Redis  │   Railway plugins, private networking only
        │ +pgvector │ │        │
        └───────────┘ └────────┘
```

| Service | Config file | Start command | Public domain |
|---|---|---|---|
| `backend` | `railway.backend.json` | `uvicorn app.api.main:app --host 0.0.0.0 --port $PORT` | **yes** |
| `ingestion` | `railway.ingestion-worker.json` | `python scripts/run_ingestion_worker.py` | no |
| `agents` | `railway.agents-worker.json` | `arq app.agents.workers.main.WorkerSettings` | no |
| `mcp` | `railway.mcp.json` | `python scripts/run_mcp_server.py` | **yes** |
| `frontend` | `frontend/railway.json` | *(nginx, image default)* | **yes** |

`backend`, `ingestion`, `agents`, and `mcp` all build from the **same root
`Dockerfile`** and differ only by start command (the same "one image, many
processes" split ENGINEERING_DECISIONS.md #002 already describes). `frontend`
builds from `frontend/Dockerfile`.

## What changed in the repo to support this

- **Postgres TLS is configurable** (`app/database/session.py`). The `ssl`
  keyword handed to asyncpg is derived from the connection string's
  `sslmode`/`ssl` parameter: `require`/unset → TLS on (Neon, unchanged),
  `?sslmode=disable` → TLS off. Railway's private network serves only a
  self-signed cert, so `?sslmode=disable` is required there — the old
  hardcoded `ssl=True` would fail the handshake.
- **MCP binds `0.0.0.0:$PORT`** and builds its transport `allowed_hosts`
  list from the environment — `MCP_PUBLIC_BASE_URL`, `RAILWAY_PUBLIC_DOMAIN`
  (auto-injected), and an optional `MCP_ALLOWED_HOSTS` — instead of the
  previously hardcoded ngrok hostname (`scripts/run_mcp_server.py`).
- **Frontend nginx listens on `$PORT`** via the nginx image's own template
  mechanism (`frontend/nginx.conf.template`, `NGINX_ENVSUBST_FILTER=^PORT$`).
- **Dockerfile / `.dockerignore`**: `README.md` (required by
  `pyproject.toml`'s `readme =`) and `scripts/run_mcp_server.py` /
  `scripts/run_api_server.py` are now in the image.
- **Railway config files** (`railway.*.json`, `frontend/railway.json`) and
  `.env.railway.example`.

The backend and workers still connect through the same `Settings` /
`app.database.session` layer as every other environment.

## 1. Create the project and the two data plugins

Railway dashboard → **New Project** → **Deploy from GitHub repo** → this
repo. Railway creates one service from the repo; you will reconfigure and
duplicate it below.

Then **+ New** → **Database** →

- **PostgreSQL** — it **must provide the `vector` extension**. Railway's
  standard PostgreSQL image includes pgvector; confirm after it starts:
  ```sql
  SELECT * FROM pg_available_extensions WHERE name = 'vector';
  ```
  If that returns no row, deploy the `pgvector/pgvector:pg16` Docker image
  as the database service instead (add a Volume mounted at
  `/var/lib/postgresql/data`). Migration `f8698cb5abae` runs
  `CREATE EXTENSION IF NOT EXISTS vector;` — it needs the extension
  *available*, not pre-created.
- **Redis** — the stock plugin is fine.

Leave both on **private networking only** (no public domain / TCP proxy
needed — every consumer is inside the project).

## 2. Configure the five application services

For the service Railway already created from the repo, and four duplicates
of it (**+ New → GitHub Repo → same repo**, or "Duplicate"), set:

| Service | Settings → Config-as-code path | Settings → Root Directory |
|---|---|---|
| `backend` | `railway.backend.json` | `/` |
| `ingestion` | `railway.ingestion-worker.json` | `/` |
| `agents` | `railway.agents-worker.json` | `/` |
| `mcp` | `railway.mcp.json` | `/` |
| `frontend` | `railway.json` | `/frontend` |

Name the services exactly `backend`, `ingestion`, `agents`, `mcp`,
`frontend` — the `${{ }}` references in `.env.railway.example` use those
names.

Generate a domain (**Settings → Networking → Generate Domain**) for
**`backend`, `frontend`, and `mcp` only**.

## 3. Variables

Copy `.env.railway.example`. The simplest layout is one **shared Variable
Group** (Project → Settings → Shared Variables) referenced by `backend`,
`ingestion`, `agents`, and `mcp`, plus a few per-service extras:

| Where | Variables |
|---|---|
| Shared (backend, ingestion, agents, mcp) | `ENVIRONMENT`, `LOG_LEVEL`, `KMS_PROVIDER`, `DATABASE_URL`, `EKIP_APP_PASSWORD`, `REDIS_URL`, `OPENAI_API_KEY`, `JWT_SECRET_KEY`, `CONNECTOR_SECRET_MASTER_KEY`, `CORS_ALLOWED_ORIGINS` |
| `backend` only | `MIGRATION_DATABASE_URL`, `EKIP_APP_ROLE_PASSWORD` |
| `mcp` only | `MCP_PUBLIC_BASE_URL` |
| `frontend` only | `VITE_API_BASE_URL` |

Two distinct database credentials, never interchangeable (same split
`docs/operations/deployment.md`'s "Migration database vs runtime database"
already documents for the Azure path):

- **`DATABASE_URL`** (shared) — the **runtime** connection every
  application process queries through, permanently. Built by hand, using
  the `ekip_app` role (`NOSUPERUSER`/`NOBYPASSRLS`, provisioned by migration
  `b8f3d6a1c4e7`), not Railway's default Postgres user:
  ```
  postgresql+asyncpg://ekip_app:${{EKIP_APP_PASSWORD}}@${{Postgres.PGHOST}}:${{Postgres.PGPORT}}/${{Postgres.PGDATABASE}}?sslmode=disable
  ```
- **`MIGRATION_DATABASE_URL`** (backend only) — the **admin** connection
  `alembic upgrade head` uses, read by `app/database/migrations/base.py`.
  `railway.backend.json`'s `preDeployCommand` runs in the same service as
  the app itself, so without this override it would inherit the shared
  block's `DATABASE_URL` (`ekip_app`) — a role migrations cannot run as,
  since it is deliberately never granted `CREATE ROLE`/`ALTER TABLE`/`GRANT`
  (those are exactly the privileges the role is scoped to *not* have):
  ```
  postgresql+asyncpg://${{Postgres.PGUSER}}:${{Postgres.PGPASSWORD}}@${{Postgres.PGHOST}}:${{Postgres.PGPORT}}/${{Postgres.PGDATABASE}}?sslmode=disable
  ```

If your Railway plan/dashboard doesn't resolve a same-group `${{EKIP_APP_PASSWORD}}`
reference, just paste the same literal generated value into both
`EKIP_APP_PASSWORD` (shared) and `EKIP_APP_ROLE_PASSWORD` (backend) instead —
they must be equal either way, since the latter is what migration
`b8f3d6a1c4e7` uses to create/converge the `ekip_app` role's password.

Generate the secrets:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"   # JWT_SECRET_KEY
python -c "import secrets; print(secrets.token_hex(32))"       # CONNECTOR_SECRET_MASTER_KEY
python -c "import secrets; print(secrets.token_urlsafe(24))"   # EKIP_APP_PASSWORD / EKIP_APP_ROLE_PASSWORD (same value, both places)
```

## 4. Deploy order

1. **`backend` first.** Its pre-deploy command is `alembic upgrade head`
   (`railway.backend.json`) — running against `MIGRATION_DATABASE_URL`
   (Railway's Postgres superuser), it creates the whole schema, the
   pgvector objects, and the `ekip_app` role (which is why
   `EKIP_APP_ROLE_PASSWORD` must be set: migration `b8f3d6a1c4e7` fails the
   deploy without it). The app itself then starts and serves traffic on
   `DATABASE_URL` (`ekip_app`), never the superuser connection.
   Watch the deploy log for the migration output, then `GET /health` and
   `GET /ready` on the backend domain, and run
   `python scripts/verify_rls_isolation.py` once (see "Verify" below) to
   confirm RLS is actually enforced under this deployment before treating
   it as production-ready.
2. **`ingestion`, `agents`, `mcp`, `frontend`** — redeploy once the schema
   exists. Order among these four does not matter.

Every later push to `main` redeploys all five automatically.

## 5. Verify

```bash
curl https://<backend-domain>/health          # {"status":"ok"}
curl https://<backend-domain>/ready           # database: ok, redis: ok
curl https://<mcp-domain>/.well-known/oauth-authorization-server   # 200 JSON
```

Then, from a shell with the deployed `DATABASE_URL` (the `ekip_app` one, not
the migration URL) in its environment — `railway run --service backend python
scripts/verify_rls_isolation.py` or the local equivalent against the same
Postgres — confirm RLS is actually enforced under the role this deployment
connects as:

```bash
railway run --service backend python scripts/verify_rls_isolation.py
```

It creates two disposable organizations with one incident each, then proves
a deliberately unscoped query (no `organization_id` filter at all) still
never returns the other organization's row, and that the connected role's
`pg_roles.rolbypassrls` is `false`. Exits non-zero with the specific failing
check if either isn't true — treat a failure here as a hard blocker, not
something to route around.

Open the frontend domain, sign up, and ask a question against a
connector-free workspace to confirm the confidence-gated "I don't know" path
before connecting real sources. Check `ingestion` and `agents` logs for a
clean Redis connect and their queue names (`arq:queue:ingestion`,
`arq:queue:agents`).

For MCP, add the `mcp` domain as a custom connector in Claude
(`https://<mcp-domain>/mcp`) and complete the OAuth flow — `MCP_PUBLIC_BASE_URL`
must equal `https://<mcp-domain>` exactly.

## Known limitations of this path

- **`ENVIRONMENT=development`.** Required by
  `_reject_local_kms_in_production` while `KMS_PROVIDER=local`. Connector
  credentials are encrypted with a master key held in an env var, not a
  separate KMS trust boundary (see `app/shared/security/kms.py`).
- **No pre-deploy private-network guarantee.** If `alembic upgrade head`
  in `backend`'s pre-deploy cannot resolve `*.railway.internal`, run it once
  from your machine with `railway run --service backend alembic upgrade head`
  (or temporarily point `DATABASE_URL` at `${{Postgres.DATABASE_PUBLIC_URL}}`
  reshaped to `+asyncpg` + `?sslmode=require`), then redeploy.
- **Single replica each.** The connector lock (`app.ingestion`) tolerates
  more ingestion replicas, but the configs pin `numReplicas: 1`; raise
  deliberately.
