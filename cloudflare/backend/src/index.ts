// Worker entrypoint that routes every request into the one always-warm
// EKIP backend container instance (see ../wrangler.toml's [[containers]]
// block). `getContainer` with no session key pins all traffic to a single
// named instance -- correct for this backend, which keeps no in-process
// state (all state is Postgres/Redis) and isn't meant to horizontally scale
// across multiple container instances the way a stateful workload might.
import { Container, getContainer } from "@cloudflare/containers";
import { env } from "cloudflare:workers";

export class BackendContainer extends Container {
  defaultPort = 8000;
  // Cloudflare's default; a cold start after sleep re-runs the Dockerfile's
  // CMD (uvicorn) from scratch, which is a few seconds, not instant --
  // acceptable for infrequent traffic, a real cost for anything latency
  // sensitive. Raise this (or keep the instance warm with periodic
  // requests) if that cold start matters for your traffic pattern.
  sleepAfter = "10m";

  envVars = {
    ENVIRONMENT: env.ENVIRONMENT,
    LOG_LEVEL: env.LOG_LEVEL,
    KMS_PROVIDER: env.KMS_PROVIDER,
    DATABASE_URL: env.DATABASE_URL,
    REDIS_URL: env.REDIS_URL,
    OPENAI_API_KEY: env.OPENAI_API_KEY,
    JWT_SECRET_KEY: env.JWT_SECRET_KEY,
    CONNECTOR_SECRET_MASTER_KEY: env.CONNECTOR_SECRET_MASTER_KEY,
    CORS_ALLOWED_ORIGINS: env.CORS_ALLOWED_ORIGINS,
  };
}

interface Env {
  BACKEND_CONTAINER: DurableObjectNamespace<BackendContainer>;
  ENVIRONMENT: string;
  LOG_LEVEL: string;
  KMS_PROVIDER: string;
  DATABASE_URL: string;
  REDIS_URL: string;
  OPENAI_API_KEY: string;
  JWT_SECRET_KEY: string;
  CONNECTOR_SECRET_MASTER_KEY: string;
  CORS_ALLOWED_ORIGINS: string;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const container = getContainer(env.BACKEND_CONTAINER);
    return container.fetch(request);
  },
};
