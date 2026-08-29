# Deployment (Cloudflare + Render, no Docker)

An alternative to `docs/operations/deployment.md`'s Azure target, for when
Azure infrastructure access is unavailable (see that doc's "Current status"
— the Azure path is blocked on subscription permissions, not on anything
architectural). This path needs no Docker image build from you: Cloudflare
Pages and Render's native Python runtime both build straight from source.

## Target architecture

```
                    ┌──────────────────┐
   Internet ───────▶│ Cloudflare Pages │  static build (frontend/dist)
                    └────────┬─────────┘
                             │ HTTPS (VITE_API_BASE_URL)
                    ┌────────▼─────────┐
                    │   ekip-backend    │  Render web service
                    │  (FastAPI, GET    │  uvicorn app.api.main:app
                    │  /health, /ready) │
                    └───┬───────────┬──┘
                        │           │
              ┌─────────▼─┐   ┌────▼──────────┐
              │   Neon    │   │    Upstash     │
              │ Postgres  │   │     Redis      │
              │ (pgvector)│   │   (rediss://)  │
              └─────┬─────┘   └───┬────────┬───┘
                    │             │        │
       ┌────────────▼──┐   ┌──────▼───┐ ┌──▼────────────────┐
       │ ekip-ingestion-│   │  ekip-   │ │ (same Redis queue, │
       │ worker (Render │   │ agents-  │ │  separate queue    │
       │ worker; arq)   │   │ worker   │ │  name each)         │
       └────────────────┘   │ (Render  │ └────────────────────┘
                             │ worker;  │
                             │ arq)     │
                             └──────────┘
```

Four deployables total, matching four things this codebase already runs as
separate processes:

| Process | Entrypoint | Where |
|---|---|---|
| Frontend | `frontend/` Vite build | Cloudflare Pages |
| Backend API | `uvicorn app.api.main:app` | Render web service |
| Ingestion worker | `arq app.ingestion.workers.main.WorkerSettings` | Render background worker |
| Agents worker | `arq app.agents.workers.main.WorkerSettings` | Render background worker |

The "agents worker" runs the knowledge-gap and pattern-detection scheduled
scans (`app/agents/workers/main.py`'s `cron_jobs`) — a second, separate arq
process from the ingestion worker, on the same Redis instance but its own
queue name (`arq:queue:agents` vs ingestion's default). It exists in the
codebase today but was never added to `docker-compose.yml`'s single `worker`
service; this deployment path is the first place both run.

The LangGraph agents themselves (answer/investigation/postmortem —
`app/agents/`) are **not** a separate deployable: they execute synchronously
inside the backend process while it handles `/ask` and
`/incidents/{id}/investigate` requests, exactly as in the Azure design.

## Prerequisites

- A [Neon](https://neon.tech) Postgres project, `vector` extension enabled
  (`CREATE EXTENSION IF NOT EXISTS vector;`) — Neon's free tier supports it.
- An [Upstash](https://upstash.com) Redis database (or any Redis reachable
  over TLS) — grab its `rediss://` connection string.
- A [Render](https://render.com) account, this repo connected.
- A [Cloudflare](https://dash.cloudflare.com) account, Pages enabled.

## 1. Backend + both workers (Render)

`render.yaml` at the repo root is a Render Blueprint defining all three
Python services. In the Render dashboard: **New +** → **Blueprint** → pick
this repo → Render reads `render.yaml` automatically.

Before the first deploy succeeds, fill in the `sync: false` values in the
`ekip-shared` env var group (Render dashboard → the group, not this file):

| Var | Value |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://<user>:<pass>@<neon-host>/<db>?ssl=require` |
| `REDIS_URL` | `rediss://default:<password>@<upstash-host>:<port>` |
| `OPENAI_API_KEY` | your real key |
| `JWT_SECRET_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `CONNECTOR_SECRET_MASTER_KEY` | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `CORS_ALLOWED_ORIGINS` | your Cloudflare Pages URL, e.g. `https://ekip-frontend.pages.dev` (set after step 2) |

All three services (`ekip-backend`, `ekip-ingestion-worker`,
`ekip-agents-worker`) read this same group — every one of them constructs
`Settings()` at startup and needs the required fields (`DATABASE_URL`,
`REDIS_URL`, `OPENAI_API_KEY`, `JWT_SECRET_KEY`) present, not just the
backend.

`ekip-backend`'s `preDeployCommand` runs `alembic upgrade head` once per
deploy before the new instance takes traffic (Starter plan and above; on
Render's Free plan there's no pre-deploy hook — run
`uv run alembic upgrade head` by hand from the dashboard's Shell tab after
the first deploy instead).

**KMS caveat**: `render.yaml` sets `KMS_PROVIDER=local`, which
`app/shared/config/settings.py` refuses whenever `ENVIRONMENT=production`
(`_reject_local_kms_in_production`) — and `render.yaml` also sets
`ENVIRONMENT=production`. Pick one before deploying:

- Set `ENVIRONMENT=production` and switch to `KMS_PROVIDER=azure` (needs a
  real Azure Key Vault plus `azure_key_vault_url`/`azure_key_vault_key_name`
  and a service principal's credentials as env vars, since
  `DefaultAzureCredential` won't have a Render-native managed identity to
  fall back to — this reintroduces a real Azure dependency, just for
  connector-secret envelope encryption, not compute), **or**
- Leave `KMS_PROVIDER=local` and set `ENVIRONMENT=development` instead in
  the shared env group, accepting `LocalKeyManagementService`'s weaker
  guarantee (see that class's own docstring) for this deployment.

Not resolved here — this is a real security tradeoff the settings module
itself is built to force a deliberate choice on, not a default to pick
silently.

## 2. Frontend (Cloudflare Pages)

In the Cloudflare dashboard: **Workers & Pages** → **Create** → **Pages** →
**Connect to Git** → this repo.

| Setting | Value |
|---|---|
| Root directory | `frontend` |
| Build command | `npm run build` |
| Build output directory | `dist` |

Environment variables (Pages project → Settings → Environment variables):

| Var | Value |
|---|---|
| `VITE_API_BASE_URL` | `ekip-backend`'s Render URL, e.g. `https://ekip-backend.onrender.com` |
| `VITE_USE_MOCK_DATA` | `false` |

`frontend/public/_redirects` (`/* /index.html 200`) makes Pages serve
`index.html` for every client-side route, the same job `nginx.conf`'s
`try_files ... /index.html` does in the Docker build — Pages has no nginx
config of its own to read.

After the first Pages deploy, copy its `https://<project>.pages.dev` URL
back into Render's `CORS_ALLOWED_ORIGINS` (step 1) — the backend rejects
credentialed cross-origin requests from any origin not on that list
(`app/shared/config/settings.py`'s `_reject_wildcard_origin`: no wildcard is
ever accepted).

## 3. Verify

```bash
curl https://ekip-backend.onrender.com/health
curl https://ekip-backend.onrender.com/ready     # checks DB/Redis reachability
```

Then open the Pages URL, sign up a user (`POST /auth/signup` via the UI),
and ask a question against a connector-free workspace to confirm the
confidence-gated "I don't know" path works before connecting real sources.

## What this path does not give you

- **Render's free/starter plans have no VNet/private networking** — Neon and
  Upstash are reached over the public internet (both are TLS-only by
  default, which is the load-bearing protection here, same principle as
  `docs/operations/deployment.md`'s Azure TLS requirements).
- **No managed identity** — unlike the Azure design's shared user-assigned
  identity for Key Vault access, this path's secrets are plain Render
  environment variables. Acceptable for a smaller/non-regulated deployment;
  revisit if the KMS caveat above pushes you toward real Key Vault access.
- **Render's free instances spin down after inactivity** — the first request
  after idle will be slow (cold start); the `starter` plan in `render.yaml`
  avoids this but costs more than free.
