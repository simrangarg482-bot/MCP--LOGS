import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * Browser tests for the tenant Observability page (`/observability`): sidebar visibility, direct-URL
 * authorization, the Agents / MCP Tools / Ingestion tabs, empty states, API failures, the time window,
 * refresh, the old /agents and /mcp URLs, and the Audit Log staying where it is.
 *
 * Like `password-reset-ui.spec.ts` the backend is a small stateful fake (`page.route`), so this needs only
 * the Vite DEV server built with `VITE_USE_MOCK_DATA=false VITE_API_BASE_URL=http://127.0.0.1:8010`.
 * Permission enforcement and tenant isolation of the endpoints themselves are server-side concerns
 * (`tests/core/observability/*`, `tests/api/*`); `observability-real.spec.ts` checks the page against the real API.
 */

const API = process.env.E2E_API_BASE_URL ?? "http://127.0.0.1:8010";
const ORG = "11111111-1111-4111-8111-111111111111";
const ME = "22222222-2222-4222-8222-222222222222";
const CONNECTOR = "abcdef12-0000-4000-8000-000000000001";

type Fail = number | undefined;

interface Backend {
  permissions: string[];
  agents: unknown[] | Fail;
  mcpTools: unknown[] | Fail;
  mcpUsage: unknown[] | Fail;
  ingestion: unknown[] | Fail;
  queue: Record<string, unknown> | Fail;
  storage: Record<string, unknown> | Fail;
}

const AGENT = {
  agent_name: "answer_question", execution_count: 10, succeeded_count: 9, failed_count: 1,
  avg_confidence_score: 0.8, avg_latency_seconds: 2.5, total_prompt_tokens: 100, total_completion_tokens: 50,
  total_tokens: 150, estimated_cost_usd: 0.0123,
};
const TOOL = {
  name: "search_knowledge", description: "Search the knowledge base.", status: "available",
  synced_at: "2026-11-01T10:00:00Z",
  parameters: [{ name: "query", type: "string", required: true, default: null, description: "What to search" }],
};
const USAGE = { tool_name: "search_knowledge", request_count: 7, error_count: 1, avg_latency_ms: 120, max_latency_ms: 900 };
const STORAGE = {
  available: true, taken_at: "2026-11-27T01:00:00Z", chunk_rows: 48210, document_rows: 3120, estimated_bytes: 182_000_000,
  is_estimate: true, basis: "row_share_of_chunk_and_document_tables",
  history: [{ taken_at: "2026-11-25T01:00:00Z", estimated_bytes: 170_000_000 }, { taken_at: "2026-11-26T01:00:00Z", estimated_bytes: 176_000_000 }, { taken_at: "2026-11-27T01:00:00Z", estimated_bytes: 182_000_000 }],
};
const INGESTION = {
  connector_config_id: CONNECTOR, run_count: 5, succeeded_count: 3, failed_count: 1, dead_lettered_count: 1,
  avg_duration_seconds: 12.5, total_documents_processed: 40, total_pages_fetched: 8, total_items_discovered: 50,
  total_items_skipped: 4, total_chunks_embedded: 200, total_retries: 2,
};

function makeBackend(initial: Partial<Backend> = {}) {
  const state: Backend = {
    permissions: ["incident:read", "observability:read", "audit:read", "tenancy:manage"],
    agents: [AGENT], mcpTools: [TOOL], mcpUsage: [USAGE], ingestion: [INGESTION],
    queue: { queued_jobs: 3, oldest_queued_age_seconds: 125, worker_max_concurrency: 4 },
    storage: STORAGE,
    ...initial,
  };
  const calls: Array<{ path: string; query: string }> = [];
  const json = (route: Route, status: number, body: unknown) =>
    route.fulfill({ status, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(body) });
  const claims = () => Buffer.from(JSON.stringify({ sub: ME, organization_id: ORG, exp: 4102444800 })).toString("base64url");
  const failure = (route: Route, status: number) =>
    json(route, status, { error_code: status === 503 ? "service.queue_unavailable" : "internal_error", message: status === 503 ? "The background job queue is temporarily unavailable." : "boom", detail: null });
  const answer = (route: Route, value: unknown) => (typeof value === "number" ? failure(route, value) : json(route, 200, value));

  async function handle(route: Route) {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname;
    if (req.method() === "OPTIONS") {
      return route.fulfill({
        status: 204,
        headers: { "access-control-allow-origin": "*", "access-control-allow-headers": "*", "access-control-allow-methods": "GET,POST,OPTIONS" },
      });
    }
    calls.push({ path, query: url.search });
    if (path === "/auth/refresh") return json(route, 200, { access_token: `h.${claims()}.s`, refresh_token: "rt2", token_type: "bearer", expires_in: 3600 });
    if (path === "/auth/me") return json(route, 200, { id: ME, email: "me@example.com", display_name: "Me", is_active: true, roles: ["admin"], permissions: state.permissions });
    if (path === "/auth/organizations") return json(route, 200, { organizations: [{ id: ORG, name: "Acme", slug: "acme" }] });
    if (path === "/organizations") return json(route, 200, [{ id: ORG, name: "Acme", slug: "acme" }]);
    if (path === "/observability/agents") return answer(route, state.agents);
    if (path === "/observability/mcp/tools") return answer(route, state.mcpTools);
    if (path === "/observability/mcp") return answer(route, state.mcpUsage);
    if (path === "/observability/storage") return answer(route, state.storage);
    if (path === "/observability/ingestion/queue") return answer(route, state.queue);
    if (path === "/observability/ingestion") return answer(route, state.ingestion);
    if (path === "/tenancy/connectors") {
      return json(route, 200, [{ id: CONNECTOR, organization_id: ORG, project_id: null, source: "github", config: {}, status: "active", last_synced_at: null, created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z" }]);
    }
    if (path.endsWith("/audit")) return json(route, 200, []);
    return json(route, 200, []);
  }

  return {
    state,
    calls,
    handle,
    callsTo: (p: string) => calls.filter((c) => c.path === p),
    observabilityCalls: () => calls.filter((c) => c.path.startsWith("/observability")),
  };
}

async function open(page: Page, initial: Partial<Backend> = {}) {
  const backend = makeBackend(initial);
  await page.addInitScript(() => localStorage.setItem("ekip.refresh_token", "rt1"));
  await page.route(`${API}/**`, backend.handle);
  return backend;
}

const nav = (page: Page) => page.getByRole("navigation", { name: "Primary navigation" });

// ======================================================================= sidebar and direct access

test.describe("Sidebar and access", () => {
  test("shows ONE Observability item (and no separate Agents / MCP Tools items) with observability:read", async ({ page }) => {
    await open(page);
    await page.goto("/ask");
    await expect(nav(page).getByRole("link", { name: "Observability" })).toBeVisible();
    await expect(nav(page).getByRole("link", { name: "Agents" })).toHaveCount(0);
    await expect(nav(page).getByRole("link", { name: "MCP Tools" })).toHaveCount(0);
    await expect(nav(page).getByRole("link", { name: "Audit Log" })).toBeVisible(); // stays where it was
  });

  test("hides the item without observability:read", async ({ page }) => {
    await open(page, { permissions: ["incident:read", "audit:read"] });
    await page.goto("/ask");
    await expect(nav(page).getByRole("link", { name: "Audit Log" })).toBeVisible();
    await expect(nav(page).getByRole("link", { name: "Observability" })).toHaveCount(0);
  });

  test("a direct visit without observability:read is denied and makes no observability request", async ({ page }) => {
    const backend = await open(page, { permissions: ["incident:read"] });
    await page.goto("/observability?tab=ingestion");
    await expect(page.getByTestId("observability-denied")).toContainText("observability:read");
    await expect(page.getByRole("tab", { name: "Ingestion" })).toHaveCount(0);
    expect(backend.observabilityCalls()).toHaveLength(0);
    // the old URLs land on the same denial
    await page.goto("/agents");
    await expect(page.getByTestId("observability-denied")).toBeVisible();
    await page.goto("/mcp");
    await expect(page.getByTestId("observability-denied")).toBeVisible();
    expect(backend.observabilityCalls()).toHaveLength(0);
  });

  test("the item opens the page on the Agents tab", async ({ page }) => {
    await open(page);
    await page.goto("/ask");
    await nav(page).getByRole("link", { name: "Observability" }).click();
    await expect(page).toHaveURL(/\/observability/);
    await expect(page.getByRole("tab", { name: "Agents" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByTestId("agent-stats")).toBeVisible();
  });
});

// ======================================================================= old URLs

test.describe("Existing Agents and MCP Tools URLs", () => {
  test("/agents opens the Agents tab with the same data", async ({ page }) => {
    await open(page);
    await page.goto("/agents");
    await expect(page).toHaveURL(/\/observability\?tab=agents/);
    await expect(page.getByRole("tab", { name: "Agents" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByTestId("agent-stats")).toContainText("answer_question");
    await expect(page.getByText("How a question is answered")).toBeVisible();
  });

  test("/mcp opens the MCP Tools tab with the catalogue and usage", async ({ page }) => {
    await open(page);
    await page.goto("/mcp");
    await expect(page).toHaveURL(/\/observability\?tab=mcp/);
    await expect(page.getByRole("heading", { name: /Registered tools \(1\)/ })).toBeVisible();
    const card = page.getByTestId("mcp-tool-search_knowledge");
    await expect(card).toContainText("Search the knowledge base.");
    await expect(card).toContainText("query");
    await expect(card).toContainText("required");
    await expect(card.getByLabel("Recorded usage of search_knowledge")).toContainText("Calls: 7");
    await expect(card.getByLabel("Recorded usage of search_knowledge")).toContainText("Errors: 1");
  });
});

// ======================================================================= agents tab

test.describe("Agents tab", () => {
  test("shows per-agent execution stats", async ({ page }) => {
    await open(page);
    await page.goto("/observability?tab=agents");
    const stats = page.getByTestId("agent-stats");
    await expect(stats).toContainText("answer_question");
    await expect(stats).toContainText("90%"); // 9 of 10 succeeded (derived from the two real counts)
    await expect(stats).toContainText("1 failed execution");
    await expect(stats).toContainText("$0.0123");
  });

  test("explains an empty organization", async ({ page }) => {
    await open(page, { agents: [] });
    await page.goto("/observability?tab=agents");
    await expect(page.getByText("No agent executions recorded yet")).toBeVisible();
    await expect(page.getByText("Statistics appear here once EKIP")).toBeVisible();
  });

  test("an API failure is reported with a working Retry", async ({ page }) => {
    const backend = await open(page, { agents: 500 });
    await page.goto("/observability?tab=agents");
    await expect(page.getByRole("alert").filter({ hasText: "Could not load agent stats" })).toBeVisible();
    backend.state.agents = [AGENT];
    await page.getByRole("button", { name: "Retry" }).click();
    await expect(page.getByTestId("agent-stats")).toBeVisible();
  });
});

// ======================================================================= mcp tab

test.describe("MCP Tools tab", () => {
  test("lists tools and usage; usage failing never hides the catalogue", async ({ page }) => {
    await open(page, { mcpUsage: 500 });
    await page.goto("/observability?tab=mcp");
    const card = page.getByTestId("mcp-tool-search_knowledge");
    await expect(card).toBeVisible();
    await expect(card.getByLabel("Recorded usage of search_knowledge")).toContainText("Usage statistics are unavailable.");
  });

  test("explains no published tools and a catalogue failure", async ({ page }) => {
    const backend = await open(page, { mcpTools: [] });
    await page.goto("/observability?tab=mcp");
    await expect(page.getByText("No MCP tools published yet")).toBeVisible();
    backend.state.mcpTools = 500;
    await page.getByRole("button", { name: "Refresh observability data" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "Could not load MCP tools" })).toBeVisible();
  });

  test("a tool with no calls says so", async ({ page }) => {
    await open(page, { mcpUsage: [] });
    await page.goto("/observability?tab=mcp");
    await expect(page.getByTestId("mcp-tool-search_knowledge")).toContainText("No recorded calls from your organization.");
  });
});

// ======================================================================= ingestion tab

test.describe("Ingestion tab", () => {
  test("shows queue health and per-connector statistics exactly as the API returns them", async ({ page }) => {
    await open(page);
    await page.goto("/observability?tab=ingestion");

    await expect(page.getByTestId("queue-depth")).toHaveText("3");
    await expect(page.getByTestId("queue-oldest")).toHaveText("2m 5s");
    await expect(page.getByTestId("queue-concurrency")).toHaveText("4");
    await expect(page.getByText("does not include jobs that are already running")).toBeVisible();

    const card = page.getByTestId(`ingestion-connector-${CONNECTOR}`);
    await expect(card).toContainText("github"); // name resolved from the connectors list (tenancy:manage)
    await expect(card).toContainText("5 runs");
    for (const [label, value] of [
      ["Succeeded", "3"], ["Failed", "1"], ["Dead-lettered", "1"], ["Retries", "2"],
      ["Documents processed", "40"], ["Pages fetched", "8"], ["Chunks embedded", "200"],
      ["Items discovered", "50"], ["Items skipped", "4"],
    ] as const) {
      await expect(card.locator("div", { has: page.locator("dt", { hasText: label }) }).first()).toContainText(value);
    }
    await expect(card).toContainText("12.5s"); // average duration of successful runs
    await expect(page.getByRole("link", { name: "Run history per connector" })).toBeVisible();
  });

  test("shows no connector name or history link without tenancy:manage, and never asks for the connector list", async ({ page }) => {
    const backend = await open(page, { permissions: ["incident:read", "observability:read"] });
    await page.goto("/observability?tab=ingestion");
    const card = page.getByTestId(`ingestion-connector-${CONNECTOR}`);
    await expect(card).toContainText("abcdef12"); // the id, shortened
    await expect(card).not.toContainText("github");
    await expect(page.getByText("only shown to people who can manage connectors")).toBeVisible();
    await expect(page.getByRole("link", { name: "Run history per connector" })).toHaveCount(0);
    expect(backend.callsTo("/tenancy/connectors")).toHaveLength(0);
  });

  test("an empty queue says nothing is waiting, without inventing a health state", async ({ page }) => {
    await open(page, { queue: { queued_jobs: 0, oldest_queued_age_seconds: null, worker_max_concurrency: 2 } });
    await page.goto("/observability?tab=ingestion");
    await expect(page.getByTestId("queue-depth")).toHaveText("0");
    await expect(page.getByTestId("queue-oldest")).toHaveText("None waiting");
    await expect(page.getByText(/healthy|degraded|unhealthy/i)).toHaveCount(0);
  });

  test("an organization with no ingestion explains it", async ({ page }) => {
    await open(page, { ingestion: [] });
    await page.goto("/observability?tab=ingestion");
    await expect(page.getByText("No ingestion runs recorded yet")).toBeVisible();
    await expect(page.getByText("Statistics appear once a connector has synced")).toBeVisible();
  });

  test("an unreachable job queue (503) is explained while the run statistics still show", async ({ page }) => {
    await open(page, { queue: 503 });
    await page.goto("/observability?tab=ingestion");
    const unavailable = page.getByTestId("queue-unavailable");
    await expect(unavailable).toContainText("Queue status is unavailable");
    await expect(unavailable).toContainText("cannot be reached");
    await expect(page.getByTestId(`ingestion-connector-${CONNECTOR}`)).toBeVisible();
    await expect(page.getByTestId("queue-health")).toHaveCount(0);
  });

  test("a statistics failure is reported with a working Retry, and does not hide the queue", async ({ page }) => {
    const backend = await open(page, { ingestion: 500 });
    await page.goto("/observability?tab=ingestion");
    await expect(page.getByRole("alert").filter({ hasText: "Could not load ingestion statistics" })).toBeVisible();
    await expect(page.getByTestId("queue-depth")).toHaveText("3");
    backend.state.ingestion = [INGESTION];
    await page.getByRole("button", { name: "Retry" }).first().click();
    await expect(page.getByTestId("ingestion-stats")).toBeVisible();
  });

  test("a non-503 queue failure is reported as a failure, with Retry", async ({ page }) => {
    const backend = await open(page, { queue: 500 });
    await page.goto("/observability?tab=ingestion");
    await expect(page.getByTestId("queue-unavailable")).toContainText("Could not read the job queue");
    backend.state.queue = { queued_jobs: 1, oldest_queued_age_seconds: 5, worker_max_concurrency: 4 };
    await page.getByTestId("queue-unavailable").getByRole("button", { name: "Retry" }).click();
    await expect(page.getByTestId("queue-depth")).toHaveText("1");
    await expect(page.getByTestId("queue-oldest")).toHaveText("5s");
  });
});

// ======================================================================= window, refresh, tabs

test.describe("Time window, refresh and tabs", () => {
  test("the time window is sent as `since` to the endpoints that support it, and not to the catalogue or queue", async ({ page }) => {
    const backend = await open(page);
    await page.goto("/observability?tab=agents");
    await expect(page.getByTestId("agent-stats")).toBeVisible();
    expect(backend.callsTo("/observability/agents")[0].query).toBe(""); // all time: no parameter

    await page.getByLabel("Time window").selectOption("7d");
    await expect(page).toHaveURL(/window=7d/);
    await expect.poll(() => backend.callsTo("/observability/agents").some((c) => c.query.includes("since="))).toBe(true);
    const since = new URL(`http://x/${backend.callsTo("/observability/agents").find((c) => c.query.includes("since="))!.query}`).searchParams.get("since")!;
    const ageDays = (Date.now() - new Date(since).getTime()) / 86_400_000;
    expect(ageDays).toBeGreaterThan(6.9);
    expect(ageDays).toBeLessThan(7.1);

    await page.getByRole("tab", { name: "MCP Tools" }).click();
    await expect(page.getByTestId("mcp-tool-search_knowledge")).toBeVisible();
    expect(backend.callsTo("/observability/mcp").some((c) => c.query.includes("since="))).toBe(true);
    expect(backend.callsTo("/observability/mcp/tools").every((c) => c.query === "")).toBe(true);

    await page.getByRole("tab", { name: "Ingestion" }).click();
    await expect(page.getByTestId("queue-depth")).toBeVisible();
    expect(backend.callsTo("/observability/ingestion").some((c) => c.query.includes("since="))).toBe(true);
    expect(backend.callsTo("/observability/ingestion/queue").every((c) => c.query === "")).toBe(true);
  });

  test("an empty window explains that a longer window may help", async ({ page }) => {
    await open(page, { agents: [] });
    await page.goto("/observability?tab=agents&window=24h");
    await expect(page.getByText("No agent executions in this time window")).toBeVisible();
  });

  test("Refresh re-reads the data", async ({ page }) => {
    const backend = await open(page);
    await page.goto("/observability?tab=ingestion");
    await expect(page.getByTestId("queue-depth")).toHaveText("3");
    backend.state.queue = { queued_jobs: 9, oldest_queued_age_seconds: 3700, worker_max_concurrency: 4 };
    await page.getByRole("button", { name: "Refresh observability data" }).click();
    await expect(page.getByTestId("queue-depth")).toHaveText("9");
    await expect(page.getByTestId("queue-oldest")).toHaveText("1h 1m");
  });

  test("tabs are keyboard operable and keep the URL in sync", async ({ page }) => {
    await open(page);
    await page.goto("/observability");
    await page.getByRole("tab", { name: "Agents" }).focus();
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("tab", { name: "MCP Tools" })).toHaveAttribute("aria-selected", "true");
    await expect(page).toHaveURL(/tab=mcp/);
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("tab", { name: "Ingestion" })).toHaveAttribute("aria-selected", "true");
    await expect(page).toHaveURL(/tab=ingestion/);
    await page.keyboard.press("End"); // Storage is now the last tab
    await expect(page.getByRole("tab", { name: "Storage" })).toHaveAttribute("aria-selected", "true");
    await expect(page).toHaveURL(/tab=storage/);
  });

  test("an unknown tab or window in the URL falls back to the defaults", async ({ page }) => {
    await open(page);
    await page.goto("/observability?tab=nope&window=forever");
    await expect(page.getByRole("tab", { name: "Agents" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByLabel("Time window")).toHaveValue("all");
  });

  test("is usable on a phone-sized screen", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 800 });
    await open(page);
    await page.goto("/observability?tab=ingestion");
    await expect(page.getByTestId("queue-depth")).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });
});

// ======================================================================= regressions

test.describe("Audit Log and Dashboard regression", () => {
  test("the Audit Log keeps its place, permission and page", async ({ page }) => {
    await open(page);
    await page.goto("/audit");
    await expect(page.getByRole("heading", { name: "Audit Log" })).toBeVisible();
    await expect(nav(page).getByRole("link", { name: "Audit Log" })).toBeVisible();
  });

  test("the Audit Log stays hidden without audit:read, independent of observability:read", async ({ page }) => {
    await open(page, { permissions: ["incident:read", "observability:read"] });
    await page.goto("/ask");
    await expect(nav(page).getByRole("link", { name: "Observability" })).toBeVisible();
    await expect(nav(page).getByRole("link", { name: "Audit Log" })).toHaveCount(0);
  });

  test("the Dashboard still loads its agent statistics (without a stray `since`)", async ({ page }) => {
    const backend = await open(page);
    await page.goto("/dashboard");
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
    await expect.poll(() => backend.callsTo("/observability/agents").length).toBeGreaterThan(0);
    expect(backend.callsTo("/observability/agents").every((c) => !c.query.includes("since"))).toBe(true);
  });
});

// ======================================================================= storage tab

test.describe("Storage tab", () => {
  test("shows this organization's estimate, labelled as an estimate, with exact row counts", async ({ page }) => {
    const backend = await open(page);
    await page.goto("/observability?tab=storage");
    await expect(page.getByTestId("storage-tab")).toBeVisible();
    await expect(page.getByTestId("storage-estimate-badge")).toHaveText("Estimate");
    await expect(page.getByTestId("storage-estimated-bytes")).toHaveText("174 MiB");
    await expect(page.getByTestId("storage-basis")).toContainText("This is an estimate, not a measurement");
    await expect(page.getByTestId("storage-chunk-rows")).toHaveText("48,210");
    await expect(page.getByTestId("storage-document-rows")).toHaveText("3,120");
    await expect(page.getByTestId("storage-history")).toBeVisible();
    // nothing about the whole database or a limit is shown to a tenant
    const html = (await page.content()).toLowerCase();
    expect(html).not.toContain("database_bytes");
    expect(html).not.toContain("storage limit");
    expect(backend.callsTo("/observability/storage").length).toBe(1);
  });

  test("before the first daily snapshot it says so instead of showing zeros", async ({ page }) => {
    await open(page, { storage: { available: false, taken_at: null, chunk_rows: 0, document_rows: 0, estimated_bytes: null, is_estimate: true, basis: "x", history: [] } });
    await page.goto("/observability?tab=storage");
    await expect(page.getByText("No storage snapshot yet")).toBeVisible();
    await expect(page.getByTestId("storage-estimated-bytes")).toHaveCount(0);
  });

  test("a failed request is reported with a working Retry", async ({ page }) => {
    const backend = await open(page, { storage: 500 });
    await page.goto("/observability?tab=storage");
    await expect(page.getByText("Could not load storage usage")).toBeVisible();
    backend.state.storage = STORAGE;
    await page.getByRole("button", { name: "Retry" }).click();
    await expect(page.getByTestId("storage-estimated-bytes")).toBeVisible();
  });

  test("the tab is reachable from the tab list and needs no extra permission request", async ({ page }) => {
    const backend = await open(page);
    await page.goto("/observability");
    await page.getByRole("tab", { name: "Storage" }).click();
    await expect(page).toHaveURL(/tab=storage/);
    await expect(page.getByTestId("storage-tab")).toBeVisible();
    expect(backend.callsTo("/observability/storage").length).toBe(1);
  });
});
