import { existsSync, readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, devices } from "@playwright/test";

const __dirname = dirname(fileURLToPath(import.meta.url));

/**
 * Loads the repo-root `.env` (gitignored, already the project's one source
 * of truth for local secrets/config -- see `scripts/test_connectors.py`'s
 * identical convention) so `e2e/integrations.spec.ts` can read
 * `EKIP_TEST_GITHUB_TOKEN`/`EKIP_TEST_GITHUB_REPOS` for a real connector
 * connect+sync, without hardcoding a credential in a committed file or
 * adding a `dotenv` dependency for one small parse.
 */
function loadRootEnv(): void {
  const path = resolve(__dirname, "../.env");
  if (!existsSync(path)) return;
  for (const line of readFileSync(path, "utf-8").split("\n")) {
    const match = /^([A-Z0-9_]+)=(.*)$/.exec(line.trim());
    if (!match) continue;
    const [, key, value] = match;
    if (process.env[key] === undefined) process.env[key] = value;
  }
}

loadRootEnv();

/**
 * Points at manually-started dev processes (frontend/e2e/README.md documents
 * exact commands) rather than an auto-managed `webServer` -- the backend is
 * a separate Python process (uvicorn + an arq worker) Playwright cannot
 * launch itself, so both sides are started once and left running for the
 * whole suite.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"], ["html", { open: "never", outputFolder: "e2e-report" }]],
  // Generous: this suite exercises a real, remote (Neon) dev database --
  // signup alone (new org + project + user + password hash + role/
  // permission bootstrap + session issuance) has been observed to take
  // 10-20s end-to-end, not the sub-second latency a mocked backend would give.
  timeout: 60_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:5180",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
