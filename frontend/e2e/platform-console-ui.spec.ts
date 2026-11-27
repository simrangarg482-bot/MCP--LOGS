import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * Browser tests for the platform-operator console (`/platform/*`).
 *
 * Like `password-reset-ui.spec.ts` this answers every backend call with a small stateful fake
 * (`page.route`), so it needs only the Vite DEV server built with
 * `VITE_USE_MOCK_DATA=false VITE_API_BASE_URL=http://127.0.0.1:8010`. The security rules themselves
 * (separate token audience, per-request operator re-verification, kill switch, transition rules,
 * history privacy) are enforced and tested server-side (`tests/api/test_platform_*`); this file proves
 * the UI honours them and never stores or reuses credentials it must not.
 */

const API = process.env.E2E_API_BASE_URL ?? "http://127.0.0.1:8010";
const OPERATOR = "33333333-3333-4333-8333-333333333333";

type Status = "onboarding" | "active" | "suspended";

interface Org {
  id: string;
  name: string;
  slug: string;
  status: Status;
  status_changed_at: string | null;
  status_message_public: string | null;
  created_at: string;
  member_count: number;
  session_epoch: number;
  history: Array<Record<string, unknown>>;
}

interface Options {
  available?: boolean;
  role?: "admin" | "viewer";
  loginError?: { status: number; error_code: string; message: string };
  /** Make every authenticated platform call fail with this code (a revoked operator, kill switch...). */
  failAuthenticatedWith?: { status: number; error_code: string };
  transitionError?: { status: number; error_code: string; message: string };
}

function makeOrgs(): Org[] {
  const mk = (n: number, name: string, status: Status, members: number): Org => ({
    id: `00000000-0000-4000-8000-00000000000${n}`,
    name,
    slug: name.toLowerCase().replace(/\s+/g, "-"),
    status,
    status_changed_at: status === "onboarding" ? null : "2026-11-01T10:00:00Z",
    status_message_public: status === "suspended" ? "Please contact billing" : null,
    created_at: `2026-10-0${n}T09:00:00Z`,
    member_count: members,
    session_epoch: status === "suspended" ? 1 : 0,
    history:
      status === "suspended"
        ? [
            {
              id: `h-${n}`, previous_status: "active", new_status: "suspended", reason: "Invoice overdue",
              customer_message: "Please contact billing", internal_note: "CFO unreachable",
              operator_user_id: OPERATOR, operator_email: "ops@ekip.test", changed_at: "2026-11-01T10:00:00Z", request_id: null,
            },
          ]
        : [],
  });
  return [
    mk(1, "Acme Corp", "active", 12),
    mk(2, "Globex", "onboarding", 3),
    mk(3, "Initech", "suspended", 7),
    mk(4, "Umbrella", "active", 40),
  ];
}

function makeBackend(options: Options = {}) {
  const orgs = makeOrgs();
  const calls: Array<{ method: string; path: string; body: unknown; query: string; auth: string | null }> = [];
  let available = options.available ?? true;
  let failAuth = options.failAuthenticatedWith;
  const json = (route: Route, status: number, body: unknown) =>
    route.fulfill({ status, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(body) });
  const token = () =>
    `h.${Buffer.from(JSON.stringify({ sub: OPERATOR, type: "platform", aud: "ekip-platform", exp: 4102444800 })).toString("base64url")}.s`;
  const detail = (o: Org) => ({
    id: o.id, name: o.name, slug: o.slug, status: o.status, status_changed_at: o.status_changed_at,
    status_message_public: o.status_message_public, created_at: o.created_at, member_count: o.member_count,
    session_epoch: o.session_epoch, history_count: o.history.length,
  });

  async function handle(route: Route) {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname;
    const method = req.method();
    if (method === "OPTIONS") {
      return route.fulfill({
        status: 204,
        headers: {
          "access-control-allow-origin": "*",
          "access-control-allow-headers": "*",
          "access-control-allow-methods": "GET,POST,OPTIONS",
        },
      });
    }
    const body = req.postData() ? JSON.parse(req.postData()!) : undefined;
    calls.push({ method, path, body, query: url.search, auth: req.headers()["authorization"] ?? null });

    if (path === "/platform/status") return json(route, 200, { available });
    if (!available) return json(route, 404, { error_code: "platform.unavailable", message: "Platform access is not available.", detail: null });
    if (path === "/platform/auth/login") {
      if (options.loginError) return json(route, options.loginError.status, { ...options.loginError, detail: null });
      return json(route, 200, { access_token: token(), token_type: "bearer", expires_in: 900, role: options.role ?? "admin" });
    }
    if (failAuth) {
      return json(route, failAuth.status, { ...failAuth, message: "denied", detail: null });
    }
    if (path === "/platform/auth/me") {
      return json(route, 200, { user_id: OPERATOR, email: "ops@ekip.test", display_name: "Ops", role: options.role ?? "admin" });
    }
    if (path === "/platform/organizations") {
      const search = (url.searchParams.get("search") ?? "").toLowerCase();
      const status = url.searchParams.get("status");
      const hits = orgs.filter(
        (o) => (!search || o.name.toLowerCase().includes(search) || o.slug.includes(search)) && (!status || o.status === status),
      );
      return json(route, 200, { items: hits.map(detail), total: hits.length, limit: 25, offset: 0 });
    }
    const m = path.match(/^\/platform\/organizations\/([^/]+)(?:\/(status-history|activate|suspend|reactivate))?$/);
    if (m) {
      const org = orgs.find((o) => o.id === m[1]);
      if (!org) return json(route, 404, { error_code: "organization.not_found", message: "Organization not found.", detail: null });
      if (!m[2]) return json(route, 200, detail(org));
      if (m[2] === "status-history") {
        return json(route, 200, { items: [...org.history].reverse(), total: org.history.length, limit: 50, offset: 0 });
      }
      if (options.transitionError) return json(route, options.transitionError.status, { ...options.transitionError, detail: null });
      const previous = org.status;
      const next: "active" | "suspended" = m[2] === "suspend" ? "suspended" : "active";
      org.status = next;
      org.status_changed_at = "2026-11-26T12:00:00Z";
      org.status_message_public = m[2] === "suspend" ? ((body as { customer_message?: string }).customer_message ?? null) : null;
      if (previous === "suspended" || next === "suspended") org.session_epoch += 1;
      org.history.push({
        id: `h-new-${org.history.length}`, previous_status: previous, new_status: next, reason: (body as { reason: string }).reason,
        customer_message: (body as { customer_message?: string }).customer_message ?? null,
        internal_note: (body as { internal_note?: string }).internal_note ?? null,
        operator_user_id: OPERATOR, operator_email: "ops@ekip.test", changed_at: "2026-11-26T12:00:00Z", request_id: null,
      });
      return json(route, 200, { organization: detail(org), previous_status: previous, new_status: next, history_id: "h", notification_queued: next === "suspended" || previous === "suspended" });
    }
    return json(route, 404, { error_code: "not_found", message: "x", detail: null });
  }

  return {
    handle,
    calls,
    callsTo: (p: string) => calls.filter((c) => c.path === p),
    setAvailable: (v: boolean) => {
      available = v;
    },
    setFailAuth: (v: { status: number; error_code: string } | undefined) => {
      failAuth = v;
    },
  };
}

async function open(page: Page, options: Options = {}) {
  const backend = makeBackend(options);
  await page.route(`${API}/**`, backend.handle);
  return backend;
}

async function signIn(page: Page) {
  await page.goto("/platform/login");
  await page.getByLabel("Operator email").fill("ops@ekip.test");
  await page.getByLabel("Password").fill("whatever password");
  await page.getByRole("button", { name: "Sign in to console" }).click();
  await expect(page.getByRole("heading", { name: "Organizations" })).toBeVisible();
}

// ======================================================================= availability / sign-in

test.describe("Platform sign-in", () => {
  test("shows an unavailable state, and no form, when the kill switch is off", async ({ page }) => {
    const backend = await open(page, { available: false });
    await page.goto("/platform/login");
    await expect(page.getByRole("heading", { name: "Platform console unavailable" })).toBeVisible();
    await expect(page.getByLabel("Operator email")).toHaveCount(0);
    // every platform URL ends up in the same state, and no login call is ever attempted
    await page.goto("/platform");
    await expect(page.getByRole("heading", { name: "Platform console unavailable" })).toBeVisible();
    expect(backend.callsTo("/platform/auth/login")).toHaveLength(0);
  });

  test("an operator signs in and lands on the organization list", async ({ page }) => {
    const backend = await open(page);
    await signIn(page);
    await expect(page.getByTestId("platform-operator")).toContainText("ops@ekip.test");
    expect(backend.callsTo("/platform/auth/login")).toHaveLength(1);
    expect(backend.callsTo("/platform/auth/login")[0].body).toEqual({ email: "ops@ekip.test", password: "whatever password" });
  });

  test("a failed sign-in shows one generic message", async ({ page }) => {
    await open(page, { loginError: { status: 403, error_code: "platform.invalid_credentials", message: "Invalid email or password." } });
    await page.goto("/platform/login");
    await page.getByLabel("Operator email").fill("someone@example.com");
    await page.getByLabel("Password").fill("nope");
    await page.getByRole("button", { name: "Sign in to console" }).click();
    await expect(page.getByRole("alert")).toHaveText("Invalid email or password.");
    await expect(page).toHaveURL(/\/platform\/login$/);
  });

  test("a rate limit is reported plainly", async ({ page }) => {
    await open(page, { loginError: { status: 429, error_code: "rate_limited.ip", message: "x" } });
    await page.goto("/platform/login");
    await page.getByLabel("Operator email").fill("a@example.com");
    await page.getByLabel("Password").fill("x");
    await page.getByRole("button", { name: "Sign in to console" }).click();
    await expect(page.getByRole("alert")).toContainText("Too many attempts");
  });

  test("an unauthenticated visit to a console page goes to the platform sign-in, not the tenant one", async ({ page }) => {
    await open(page);
    await page.goto("/platform/organizations/00000000-0000-4000-8000-000000000001");
    await expect(page).toHaveURL(/\/platform\/login$/);
  });
});

// ======================================================================= credentials handling

test.describe("Platform credentials", () => {
  test("the platform token is never persisted and a tenant token is never sent to /platform", async ({ page }) => {
    const backend = await open(page);
    // a tenant session exists in the browser (its refresh token is the only thing the tenant app persists)
    await page.addInitScript(() => localStorage.setItem("ekip.refresh_token", "tenant-refresh-token"));
    await signIn(page);

    const storage = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }) + document.cookie);
    expect(storage).not.toContain("ekip-platform"); // no platform token claims anywhere
    expect(storage).not.toMatch(/platform/i);
    // every authenticated platform call carried the platform token and nothing else
    const authed = backend.calls.filter((c) => c.path.startsWith("/platform/") && c.auth);
    expect(authed.length).toBeGreaterThan(0);
    for (const call of authed) expect(call.auth).toMatch(/^Bearer h\./);
    // the tenant refresh token (the tenant app's own bootstrap) was never presented to the platform API
    expect(backend.calls.filter((c) => c.path.startsWith("/platform/") && c.auth?.includes("tenant"))).toHaveLength(0);
    expect(JSON.stringify(backend.calls.filter((c) => c.path.startsWith("/platform/")))).not.toContain("tenant-refresh-token");
  });

  test("a reload ends the operator session (the token lives in memory only)", async ({ page }) => {
    await open(page);
    await signIn(page);
    await page.reload();
    await expect(page).toHaveURL(/\/platform\/login$/);
  });

  test("signing out clears the session", async ({ page }) => {
    await open(page);
    await signIn(page);
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page).toHaveURL(/\/platform\/login$/);
    await page.goto("/platform");
    await expect(page).toHaveURL(/\/platform\/login$/);
  });

  test("when the server revokes the session the console returns to sign-in and says so", async ({ page }) => {
    const backend = await open(page);
    await signIn(page);
    backend.setFailAuth({ status: 403, error_code: "platform.access_denied" }); // the operator was revoked
    void page.getByLabel("Search").fill("acme"); // any authenticated request; the redirect detaches the input
    await expect(page).toHaveURL(/\/platform\/login$/);
    await expect(page.getByRole("status")).toContainText("Your platform session ended");
    // and the token is gone: signing in again is the only way back
    backend.setFailAuth(undefined);
    await page.goto("/platform");
    await expect(page).toHaveURL(/\/platform\/login$/);
  });

  test("when the kill switch is turned off mid-session the console shows the unavailable state", async ({ page }) => {
    const backend = await open(page);
    await signIn(page);
    backend.setAvailable(false);
    await page.getByLabel("Search").fill("acme"); // triggers an authenticated request
    await expect(page.getByRole("heading", { name: "Platform console unavailable" })).toBeVisible();
  });
});

// ======================================================================= list, search, filter

test.describe("Organization list", () => {
  test("lists organizations with status and member count, searches and filters", async ({ page }) => {
    const backend = await open(page);
    await signIn(page);
    const table = page.getByTestId("organizations-table");
    await expect(table.getByRole("row")).toHaveCount(5); // header + 4
    await expect(table).toContainText("Acme Corp");
    await expect(table).toContainText("Suspended");
    await expect(table).toContainText("Onboarding");

    await page.getByLabel("Search").fill("glob");
    await expect(table.getByRole("row")).toHaveCount(2);
    await expect(table).toContainText("Globex");
    expect(backend.calls.some((c) => c.path === "/platform/organizations" && c.query.includes("search=glob"))).toBe(true);

    await page.getByLabel("Search").fill("");
    await page.getByLabel("Status").selectOption("suspended");
    await expect(table.getByRole("row")).toHaveCount(2);
    await expect(table).toContainText("Initech");
    expect(backend.calls.some((c) => c.query.includes("status=suspended"))).toBe(true);

    await page.getByLabel("Status").selectOption("active");
    await expect(table.getByRole("row")).toHaveCount(3);
  });

  test("an empty result is explained", async ({ page }) => {
    await open(page);
    await signIn(page);
    await page.getByLabel("Search").fill("zzzz-nothing");
    await expect(page.getByText("No organizations match")).toBeVisible();
  });
});

// ======================================================================= detail + history

test.describe("Organization detail", () => {
  test("shows details and the status history, including the operator-only note", async ({ page }) => {
    await open(page);
    await signIn(page);
    await page.getByRole("link", { name: "Initech" }).click();
    await expect(page.getByRole("heading", { name: "Initech" })).toBeVisible();
    await expect(page.getByTestId("org-status")).toHaveText("Suspended");
    await expect(page.getByText("Message shown to the customer")).toBeVisible();
    const history = page.getByTestId("status-history");
    await expect(history).toContainText("Invoice overdue");
    await expect(history).toContainText("Internal note:");
    await expect(history).toContainText("CFO unreachable");
    await expect(history).toContainText("ops@ekip.test");
  });

  test("an organization with no changes shows an empty history", async ({ page }) => {
    await open(page);
    await signIn(page);
    await page.getByRole("link", { name: "Globex" }).click();
    await expect(page.getByText("No status changes yet")).toBeVisible();
  });

  test("an unknown organization is a clear not-found", async ({ page }) => {
    await open(page);
    await signIn(page);
    // client-side navigation (a reload would end the in-memory operator session)
    await page.evaluate(() => {
      window.history.pushState({}, "", "/platform/organizations/ffffffff-ffff-4fff-8fff-ffffffffffff");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await expect(page.getByText("Organization not found")).toBeVisible();
  });
});

// ======================================================================= lifecycle actions

test.describe("Lifecycle actions", () => {
  test("only the actions the status allows are offered", async ({ page }) => {
    await open(page);
    await signIn(page);
    let first = true;
    for (const [name, buttons, hidden] of [
      ["Globex", ["Activate", "Suspend"], ["Reactivate"]],
      ["Acme Corp", ["Suspend"], ["Activate", "Reactivate"]],
      ["Initech", ["Reactivate"], ["Activate", "Suspend"]],
    ] as const) {
      // client-side navigation only: a reload would end the in-memory operator session
      if (!first) await page.getByRole("link", { name: "Organizations", exact: true }).first().click();
      first = false;
      await page.getByRole("link", { name }).click();
      for (const b of buttons) await expect(page.getByRole("button", { name: b, exact: true })).toBeVisible();
      for (const b of hidden) await expect(page.getByRole("button", { name: b, exact: true })).toHaveCount(0);
    }
  });

  test("suspending requires a reason and an explicit confirmation, then sends everything once", async ({ page }) => {
    const backend = await open(page);
    await signIn(page);
    await page.getByRole("link", { name: "Acme Corp" }).click();
    await page.getByRole("button", { name: "Suspend", exact: true }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("signed out immediately");

    // nothing filled in: the reason is required and nothing is sent
    await dialog.getByRole("button", { name: "Suspend organization" }).click();
    await expect(dialog.getByText("A reason is required.")).toBeVisible();
    expect(backend.calls.filter((c) => c.method === "POST" && c.path.endsWith("/suspend"))).toHaveLength(0);

    // reason given, confirmation missing
    await dialog.getByLabel(/^Reason/).fill("   "); // whitespace only is still empty
    await dialog.getByRole("button", { name: "Suspend organization" }).click();
    await expect(dialog.getByText("A reason is required.")).toBeVisible();
    await dialog.getByLabel(/^Reason/).fill("Invoice overdue");
    await dialog.getByRole("button", { name: "Suspend organization" }).click();
    await expect(dialog.getByText("Confirm that you understand the effect.")).toBeVisible();
    expect(backend.calls.filter((c) => c.path.endsWith("/suspend"))).toHaveLength(0);

    await dialog.getByLabel(/^Message to the customer/).fill("Please contact billing");
    await dialog.getByLabel(/^Internal note/).fill("Called twice");
    await dialog.getByRole("checkbox").check();
    await dialog.getByRole("button", { name: "Suspend organization" }).dblclick(); // a double click is one request

    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.getByTestId("org-status")).toHaveText("Suspended");
    const posts = backend.calls.filter((c) => c.method === "POST" && c.path.endsWith("/suspend"));
    expect(posts).toHaveLength(1);
    expect(posts[0].body).toEqual({ reason: "Invoice overdue", customer_message: "Please contact billing", internal_note: "Called twice" });
    await expect(page.getByTestId("status-history")).toContainText("Invoice overdue");
    await expect(page.getByTestId("status-history")).toContainText("Called twice");
    await expect(page.getByRole("button", { name: "Reactivate", exact: true })).toBeVisible();
  });

  test("the customer message and internal note are optional", async ({ page }) => {
    const backend = await open(page);
    await signIn(page);
    await page.getByRole("link", { name: "Umbrella" }).click();
    await page.getByRole("button", { name: "Suspend", exact: true }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel(/^Reason/).fill("Security review");
    await dialog.getByRole("checkbox").check();
    await dialog.getByRole("button", { name: "Suspend organization" }).click();
    await expect(page.getByTestId("org-status")).toHaveText("Suspended");
    expect(backend.calls.filter((c) => c.path.endsWith("/suspend"))[0].body).toEqual({ reason: "Security review" });
  });

  test("activation requires a reason", async ({ page }) => {
    const backend = await open(page);
    await signIn(page);
    await page.getByRole("link", { name: "Globex" }).click();
    await page.getByRole("button", { name: "Activate", exact: true }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("button", { name: "Activate organization" }).click();
    await expect(dialog.getByText("A reason is required.")).toBeVisible();
    expect(backend.calls.filter((c) => c.path.endsWith("/activate"))).toHaveLength(0);
    await dialog.getByLabel(/^Reason/).fill("Contract signed");
    await dialog.getByRole("button", { name: "Activate organization" }).click();
    await expect(page.getByTestId("org-status")).toHaveText("Active");
    expect(backend.calls.filter((c) => c.path.endsWith("/activate"))[0].body).toEqual({ reason: "Contract signed" });
  });

  test("reactivation requires a reason and does not offer the suspension-only fields", async ({ page }) => {
    const backend = await open(page);
    await signIn(page);
    await page.getByRole("link", { name: "Initech" }).click();
    await page.getByRole("button", { name: "Reactivate", exact: true }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByLabel(/^Internal note/)).toHaveCount(0);
    await expect(dialog.getByLabel(/^Message to the customer/)).toHaveCount(0);
    await dialog.getByRole("button", { name: "Reactivate organization" }).click();
    await expect(dialog.getByText("A reason is required.")).toBeVisible();
    await dialog.getByLabel(/^Reason/).fill("Paid in full");
    await dialog.getByRole("button", { name: "Reactivate organization" }).click();
    await expect(page.getByTestId("org-status")).toHaveText("Active");
    expect(backend.calls.filter((c) => c.path.endsWith("/reactivate"))[0].body).toEqual({ reason: "Paid in full" });
  });

  test("Cancel and Escape close the dialog without sending anything", async ({ page }) => {
    const backend = await open(page);
    await signIn(page);
    await page.getByRole("link", { name: "Acme Corp" }).click();
    await page.getByRole("button", { name: "Suspend", exact: true }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Cancel" }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await page.getByRole("button", { name: "Suspend", exact: true }).click();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect(backend.calls.filter((c) => c.path.endsWith("/suspend"))).toHaveLength(0);
  });

  test("a conflict from the server is explained in the dialog", async ({ page }) => {
    await open(page, { transitionError: { status: 409, error_code: "organization.already_in_status", message: "The organization is already suspended." } });
    await signIn(page);
    await page.getByRole("link", { name: "Acme Corp" }).click();
    await page.getByRole("button", { name: "Suspend", exact: true }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel(/^Reason/).fill("r");
    await dialog.getByRole("checkbox").check();
    await dialog.getByRole("button", { name: "Suspend organization" }).click();
    await expect(dialog.getByRole("alert").last()).toContainText("already suspended");
    await expect(dialog).toBeVisible(); // stays open so nothing typed is lost
  });

  test("a viewer operator can read everything but is offered no change", async ({ page }) => {
    await open(page, { role: "viewer" });
    await signIn(page);
    await expect(page.getByTestId("platform-operator")).toContainText("viewer");
    await page.getByRole("link", { name: "Acme Corp" }).click();
    await expect(page.getByText("Read-only: administrators change status.")).toBeVisible();
    for (const b of ["Suspend", "Activate", "Reactivate"]) {
      await expect(page.getByRole("button", { name: b, exact: true })).toHaveCount(0);
    }
    await expect(page.getByTestId("status-history")).toHaveCount(0); // Acme has no history
  });
});

// ======================================================================= tenant users

test.describe("Tenant users and the console", () => {
  test("a tenant session does not open the console, and the console never reads the tenant token", async ({ page }) => {
    const backend = await open(page);
    await page.addInitScript(() => localStorage.setItem("ekip.refresh_token", "tenant-refresh-token"));
    await page.goto("/platform");
    await expect(page).toHaveURL(/\/platform\/login$/);
    expect(backend.calls.filter((c) => c.path.startsWith("/platform/") && c.auth)).toHaveLength(0);
    expect(JSON.stringify(backend.calls.filter((c) => c.path.startsWith("/platform/")))).not.toContain("tenant-refresh-token");
  });

  test("the customer app has no link to the console", async ({ page }) => {
    await open(page);
    await page.goto("/login");
    await expect(page.getByRole("link", { name: /platform/i })).toHaveCount(0);
  });
});
