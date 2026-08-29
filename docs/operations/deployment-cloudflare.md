# Deployment: Cloudflare Pages + Cloudflare Containers + Render

An alternative to `docs/operations/deployment.md`'s Azure target, for when
Azure infrastructure access is unavailable (see that doc's "Current status"
— the Azure path is blocked on subscription permissions, not on anything
architectural).

**Docker is required at one point in this path**: `wrangler deploy` builds
and pushes the backend's container image via Docker (Cloudflare's own docs:
"On deploy, Wrangler uploads your Worker, builds and pushes the container
image with Docker"). The GitHub Actions workflow below runs that build on a
GitHub-hosted runner (Docker preinstalled), so it never needs to happen on
your own machine — but it is a real `docker build` somewhere, unavoidably,
for the Containers half of this deployment. Nothing else in this path
touches Docker.

## Why the two arq workers aren't on Cloudflare too

Cloudflare Containers only supports request-triggered instances: "all
Container requests are passed through a Worker" (Cloudflare's own docs), and
an idle instance sleeps after `sleepAfter` (10 minutes by default) with no
supported standalone/non-HTTP process mode. `app.ingestion.workers.main.
WorkerSettings` and `app.agents.workers.main.WorkerSettings` are both
processes that do nothing but poll Redis forever and never serve HTTP — they
architecturally do not fit the Containers model. They run on Render instead
(`render.yaml`, unchanged from that plan otherwise).

## Target architecture

```
                    ┌──────────────────┐
   Internet ───────▶│ Cloudflare Pages │  static build (frontend/dist)
                    └────────┬─────────┘
                             │ HTTPS (VITE_API_BASE_URL)
                    ┌────────▼─────────┐
                    │  Worker (thin)    │  cloudflare/backend/src/index.ts
                    │  routes every     │
                    │  request in       │
                    └────────┬─────────┘
                    ┌────────▼─────────┐
                    │ Container instance│  FastAPI (Dockerfile's default
                    │ (Durable Object-  │  CMD: uvicorn app.api.main:app)
                    │  backed)          │
                    └───┬───────────┬──┘
                        │           │
              ┌─────────▼─┐   ┌────▼──────────┐
              │   Neon    │   │    Upstash     │
              │ Postgres  │   │     Redis      │
              │ (pgvector)│   │   (rediss://)  │
              └─────┬─────┘   └───┬────────┬───┘
                    │             │        │
       ┌────────────▼──┐   ┌──────▼───┐ ┌──▼──────┐
       │ ekip-ingestion-│   │  ekip-   │ │(same     │
       │ worker (Render;│   │ agents-  │ │ Redis,   │
       │ no Docker)     │   │ worker   │ │ different│
       └────────────────┘   │ (Render) │ │ queue    │
                             └──────────┘ │ names)   │
                                          └──────────┘
```

| Process | Entrypoint | Where | Docker involved? |
|---|---|---|---|
| Frontend | `frontend/` Vite build | Cloudflare Pages | No |
| Backend API | `uvicorn app.api.main:app` (Dockerfile's default CMD) | Cloudflare Containers | Yes, at `wrangler deploy` build time only |
| Ingestion worker | `arq app.ingestion.workers.main.WorkerSettings` | Render background worker | No |
| Agents worker | `arq app.agents.workers.main.WorkerSettings` | Render background worker | No |

The LangGraph agents (`app/agents/`) are not a separate deployable — they
execute synchronously inside the backend container while it handles `/ask`
and `/incidents/{id}/investigate` requests.

## Prerequisites

- A [Neon](https://neon.tech) Postgres project, `vector` extension enabled
  (`CREATE EXTENSION IF NOT EXISTS vector;`).
- An [Upstash](https://upstash.com) Redis database (TLS, `rediss://`).
- A [Render](https://render.com) account, this repo connected, for the two
  workers only.
- A [Cloudflare](https://dash.cloudflare.com) account with Pages and
  Containers enabled, and a Cloudflare API token (Workers Scripts: Edit,
  Containers: Edit) saved as this repo's `CLOUDFLARE_API_TOKEN` GitHub
  secret.
- `DATABASE_URL` also saved as a GitHub secret (used by the `migrate` job in
  `.github/workflows/deploy-cloudflare.yml`).

## 1. Frontend — Cloudflare Pages

Cloudflare dashboard → **Workers & Pages** → **Create** → **Pages** →
**Connect to Git** → this repo.

| Setting | Value |
|---|---|
| Root directory | `frontend` |
| Build command | `npm run build` |
| Build output directory | `dist` |
| Env vars | `VITE_API_BASE_URL=<Worker URL from step 2>`, `VITE_USE_MOCK_DATA=false` |

`frontend/public/_redirects` (`/* /index.html 200`) handles SPA routing —
Pages' equivalent of `nginx.conf`'s `try_files` in the Docker build.

## 2. Backend — Cloudflare Containers

Files already in this repo: `cloudflare/backend/wrangler.toml`,
`cloudflare/backend/src/index.ts`, `cloudflare/backend/package.json`, and
`.github/workflows/deploy-cloudflare.yml`.

**One-time setup**, from your machine (needs `npm`, and Docker running
locally only for this manual first pass — every push after this deploys via
the GitHub Actions workflow instead, whose runner supplies Docker itself):

```bash
cd cloudflare/backend
npm install --save-dev wrangler
npm install @cloudflare/containers
npx wrangler login

# Secrets -- set once; wrangler prompts for each value on stdin.
npx wrangler secret put DATABASE_URL                 # postgresql+asyncpg://<user>:<pass>@<neon-host>/<db>?ssl=require
npx wrangler secret put REDIS_URL                    # rediss://default:<password>@<upstash-host>:<port>
npx wrangler secret put OPENAI_API_KEY
npx wrangler secret put JWT_SECRET_KEY               # python -c "import secrets; print(secrets.token_urlsafe(48))"
npx wrangler secret put CONNECTOR_SECRET_MASTER_KEY   # python -c "import secrets; print(secrets.token_hex(32))"
npx wrangler secret put CORS_ALLOWED_ORIGINS         # your Pages URL, e.g. https://ekip-frontend.pages.dev

npx wrangler deploy   # first deploy; note the workers.dev URL it prints
```

After this, every push to `main` touching `app/`, `Dockerfile`, or
`cloudflare/backend/` re-runs migrations and redeploys automatically via
`.github/workflows/deploy-cloudflare.yml` — no local Docker needed for those
later deploys.

**KMS caveat**: `wrangler.toml` sets `ENVIRONMENT=production` and
`KMS_PROVIDER=local`. `app/shared/config/settings.py` refuses exactly that
combination (`_reject_local_kms_in_production`) — the container will crash
at startup until you resolve this. Either:
- switch to `KMS_PROVIDER=azure` (needs a real Azure Key Vault, its
  `azure_key_vault_url`/`azure_key_vault_key_name` in `wrangler.toml`'s
  `[vars]`, and a service principal's credentials as additional
  `wrangler secret put` values, since there's no Azure-managed-identity
  fallback outside Azure compute), or
- change `ENVIRONMENT` to `"development"` in `wrangler.toml`'s `[vars]`,
  accepting `LocalKeyManagementService`'s weaker guarantee (see its own
  docstring) for this deployment.

Not resolved here — a deliberate security tradeoff, not a default to pick
silently.

## 3. Both workers — Render

`render.yaml` at the repo root defines `ekip-ingestion-worker` and
`ekip-agents-worker`. Render dashboard → **New +** → **Blueprint** → this
repo → Render reads `render.yaml` automatically. Fill in the `sync: false`
values in the `ekip-shared` env var group — same `DATABASE_URL`/`REDIS_URL`
as step 2, so both the Cloudflare-hosted backend and these two Render
workers read from and write to the same Postgres/Redis.

## 4. Verify

```bash
curl https://<worker-name>.<account>.workers.dev/health
curl https://<worker-name>.<account>.workers.dev/ready     # checks DB/Redis reachability
```

Then open the Pages URL, sign up a user, and ask a question against a
connector-free workspace to confirm the confidence-gated "I don't know" path
works before connecting real sources. Check Render's logs for both workers
to confirm they connected to Redis and picked up their respective queues
(`arq:queue:agents` for the agents worker; ingestion's default queue name
for the other).

## What this path does not give you

- **Cold starts on the backend.** After 10 minutes idle, the container
  instance sleeps; the next request re-runs the Dockerfile's `CMD` from
  scratch (a few seconds), not instant. Raise `sleepAfter` in
  `cloudflare/backend/src/index.ts` or accept the cold start.
- **No horizontal scaling of the backend by default.** `wrangler.toml` sets
  `max_instances = 1` and `src/index.ts` routes every request to one named
  instance — correct for this backend today (all state lives in Postgres/
  Redis, nothing in-process), but revisit both values together if traffic
  ever needs more than one instance.
- **No managed identity / VNet.** Neon and Upstash are reached over the
  public internet (TLS-only by default, the load-bearing protection here);
  secrets live as Cloudflare/Render environment variables, not a KMS-backed
  vault, unless you resolve the KMS caveat above.
