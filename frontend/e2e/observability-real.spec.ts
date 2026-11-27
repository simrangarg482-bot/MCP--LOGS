import { expect, test } from "@playwright/test";
import { API_BASE_URL, apiLogin, loginViaUI } from "./fixtures/actions";
import { ORG_ALPHA, ORG_BETA } from "./fixtures/testUsers";

/**
 * The Observability page against the REAL API and database: each organization sees only its own numbers,
 * what the page shows equals what the endpoints return, and a missing job queue is reported honestly.
 *
 * Needs the seeded e2e accounts (`scripts/e2e_seed.py`) and ingestion activity for ONE of them
 * (`obs_seed.py`: two runs for e2e-org-alpha, none for e2e-org-beta). Skipped unless
 * E2E_OBSERVABILITY_SEEDED=1. The job queue (Redis) is normally NOT running in this setup, which makes the
 * 503 "queue unavailable" path a real one.
 */
test.skip(process.env.E2E_OBSERVABILITY_SEEDED !== "1", "needs E2E_OBSERVABILITY_SEEDED=1 (seeded alpha ingestion data)");

interface Stats {
  connector_config_id: string;
  run_count: number;
  succeeded_count: number;
  failed_count: number;
}

test("an organization sees its own ingestion statistics, matching the API", async ({ page, request }) => {
  const token = await apiLogin(request, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);
  const api = (await (await request.get(`${API_BASE_URL}/observability/ingestion`, { headers: { Authorization: `Bearer ${token}` } })).json()) as Stats[];
  expect(api.length).toBeGreaterThan(0);

  await loginViaUI(page, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);
  await page.getByRole("navigation", { name: "Primary navigation" }).getByRole("link", { name: "Observability" }).click();
  await page.getByRole("tab", { name: "Ingestion" }).click();

  for (const row of api) {
    const card = page.getByTestId(`ingestion-connector-${row.connector_config_id}`);
    await expect(card).toBeVisible({ timeout: 30_000 });
    await expect(card).toContainText(`${row.run_count} runs`);
    await expect(card.locator("div", { has: page.locator("dt", { hasText: /^Succeeded$/ }) }).first()).toContainText(String(row.succeeded_count));
    await expect(card.locator("div", { has: page.locator("dt", { hasText: /^Failed$/ }) }).first()).toContainText(String(row.failed_count));
  }
});

test("another organization sees none of it (tenant isolation)", async ({ page, request }) => {
  const alphaToken = await apiLogin(request, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);
  const alpha = (await (await request.get(`${API_BASE_URL}/observability/ingestion`, { headers: { Authorization: `Bearer ${alphaToken}` } })).json()) as Stats[];
  const betaToken = await apiLogin(request, ORG_BETA.admin.email, ORG_BETA.admin.password);
  const beta = (await (await request.get(`${API_BASE_URL}/observability/ingestion`, { headers: { Authorization: `Bearer ${betaToken}` } })).json()) as Stats[];
  expect(alpha.length).toBeGreaterThan(0);
  expect(beta.map((r) => r.connector_config_id)).not.toContain(alpha[0].connector_config_id);

  await loginViaUI(page, ORG_BETA.admin.email, ORG_BETA.admin.password);
  await page.goto("/observability?tab=ingestion");
  for (const row of alpha) {
    await expect(page.getByTestId(`ingestion-connector-${row.connector_config_id}`)).toHaveCount(0);
  }
  if (beta.length === 0) {
    await expect(page.getByText("No ingestion runs recorded yet")).toBeVisible({ timeout: 30_000 });
  }
});

test("a missing job queue is reported as unavailable while run statistics still load", async ({ page }) => {
  await loginViaUI(page, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);
  await page.goto("/observability?tab=ingestion");
  await expect(page.getByTestId("queue-unavailable")).toContainText(/Queue status is unavailable|Could not read the job queue/, { timeout: 30_000 });
  await expect(page.getByTestId("ingestion-stats")).toBeVisible({ timeout: 30_000 });
});

test("the Agents and MCP Tools tabs load against the real API, and the old URLs still work", async ({ page, request }) => {
  const token = await apiLogin(request, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);
  const tools = (await (await request.get(`${API_BASE_URL}/observability/mcp/tools`, { headers: { Authorization: `Bearer ${token}` } })).json()) as Array<{ name: string }>;

  await loginViaUI(page, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);
  await page.goto("/agents");
  await expect(page).toHaveURL(/\/observability\?tab=agents/);
  await expect(page.getByText(/Agent execution stats/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/Could not load agent stats/)).toHaveCount(0);

  await page.goto("/mcp");
  await expect(page).toHaveURL(/\/observability\?tab=mcp/);
  if (tools.length > 0) {
    await expect(page.getByRole("heading", { name: new RegExp(`Registered tools \\(${tools.length}\\)`) })).toBeVisible({ timeout: 30_000 });
  } else {
    await expect(page.getByText("No MCP tools published yet")).toBeVisible({ timeout: 30_000 });
  }
});
