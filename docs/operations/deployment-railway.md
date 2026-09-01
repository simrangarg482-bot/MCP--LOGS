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
| Shared (backend, ingestion, agents, mcp) | `ENVIRONMENT`, `LOG_LEVEL`, `KMS_PROVIDER`, `DATABASE_URL`, `EKIP_APP_ROLE_PASSWORD`, `REDIS_URL`, `OPENAI_API_KEY`, `JWT_SECRET_KEY`, `CONNECTOR_SECRET_MASTER_KEY`, `CORS_ALLOWED_ORIGINS` |
| `mcp` only | `MCP_PUBLIC_BASE_URL` |
| `frontend` only | `VITE_API_BASE_URL` |

`DATABASE_URL` is built by hand (not `${{Postgres.DATABASE_URL}}`) so it uses
the `+asyncpg` driver and `?sslmode=disable`:

```
postgresql+asyncpg://${{Postgres.PGUSER}}:${{Postgres.PGPASSWORD}}@${{Postgres.PGHOST}}:${{Postgres.PGPORT}}/${{Postgres.PGDATABASE}}?sslmode=disable
```

Generate the three secrets:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"   # JWT_SECRET_KEY
python -c "import secrets; print(secrets.token_hex(32))"       # CONNECTOR_SECRET_MASTER_KEY
python -c "import secrets; print(secrets.token_urlsafe(24))"   # EKIP_APP_ROLE_PASSWORD
```

## 4. Deploy order

1. **`backend` first.** Its pre-deploy command is `alembic upgrade head`
   (`railway.backend.json`) — it creates the whole schema, the pgvector
   objects, and the `ekip_app` role (which is why `EKIP_APP_ROLE_PASSWORD`
   must be set: migration `b8f3d6a1c4e7` fails the deploy without it).
   Watch the deploy log for the migration output, then `GET /health` and
   `GET /ready` on the backend domain.
2. **`ingestion`, `agents`, `mcp`, `frontend`** — redeploy once the schema
   exists. Order among these four does not matter.

Every later push to `main` redeploys all five automatically.

## 5. Verify

```bash
curl https://<backend-domain>/health          # {"status":"ok"}
curl https://<backend-domain>/ready           # database: ok, redis: ok
curl https://<mcp-domain>/.well-known/oauth-authorization-server   # 200 JSON
```

Open the frontend domain, sign up, and ask a question against a
connector-free workspace to confirm the confidence-gated "I don't know" path
before connecting real sources. Check `ingestion` and `agents` logs for a
clean Redis connect and their queue names (`arq:queue:ingestion`,
`arq:queue:agents`).

For MCP, add the `mcp` domain as a custom connector in Claude
(`https://<mcp-domain>/mcp`) and complete the OAuth flow — `MCP_PUBLIC_BASE_URL`
must equal `https://<mcp-domain>` exactly.

## Known limitations of this path

- **RLS is a no-op.** Every service connects as Railway's Postgres
  superuser, which carries `BYPASSRLS` — the same situation migration
  `b8f3d6a1c4e7`'s docstring describes for Neon. The `ekip_app` role is
  still provisioned but nothing connects as it. To close this, point the
  application services' `DATABASE_URL` at `ekip_app` (same host, that role's
  password) while keeping `backend`'s pre-deploy migration URL on the
  superuser — a follow-up, not required to boot.
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
