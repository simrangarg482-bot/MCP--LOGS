import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * Browser tests for the tenant provisioning UI: Settings -> Provisioning (SCIM), the member status and
 * deactivate / reactivate actions on Settings -> Users, and the audit-log filters and readable descriptions.
 *
 * Like `password-reset-ui.spec.ts` the backend is a small stateful fake (`page.route`), so this needs only the
 * Vite DEV server built with `VITE_USE_MOCK_DATA=false VITE_API_BASE_URL=http://127.0.0.1:8010`. The rules
 * themselves (token hashing, expiry, isolation, guards, last administrator, revocation) are enforced and
 * tested server-side in `tests/api/test_scim_api_postgres.py` and friends.
 */

const API = process.env.E2E_API_BASE_URL ?? "http://127.0.0.1:8010";
const ORG = "11111111-1111-4111-8111-111111111111";
const ME = "22222222-2222-4222-8222-222222222222";
const ADA = "aaaaaaaa-0000-4000-8000-000000000001";
const BOB = "bbbbbbbb-0000-4000-8000-000000000002";
const NOW = Date.now();
const iso = (offsetMs: number) => new Date(NOW + offsetMs).toISOString();
const DAY = 86_400_000;

interface State {
  permissions: string[];
  overview: Record<string, unknown> | number;
  tokens: Array<Record<string, unknown>> | number;
  identities: Array<Record<string, unknown>>;
  pending: Array<Record<string, unknown>>;
  members: Array<Record<string, unknown>>;
  audit: Array<Record<string, unknown>>;
  groups: Array<Record<string, unknown>>;
  groupRolesError?: { status: number; error_code: string; message: string };
  memberActionError?: { status: number; error_code: string; message: string };
  tokenCreateError?: { status: number; error_code: string; message: string };
}

const baseOverview = () => ({
  platform_enabled: true, enabled: true, endpoint_url: "https://api.example.test/scim/v2",
  guard_min_absolute: 5, guard_percent: 20, guard_window_minutes: 60, reactivation_settle_seconds: 300,
  last_request_at: iso(-60_000), last_success_at: iso(-60_000), last_error_code: null, last_error_at: null,
  active_tokens: 1, pending_deactivations: 0, pending_identities: 0,
});

const token = (over: Record<string, unknown> = {}) => ({
  id: "t-1", name: "Entra ID", prefix: "scim_AbCdEfGh", created_at: iso(-10 * DAY), expires_at: iso(80 * DAY),
  last_used_at: iso(-60_000), revoked_at: null, status: "active", ...over,
});

const member = (id: string, name: string, over: Record<string, unknown> = {}) => ({
  id, email: `${name.toLowerCase()}@example.test`, display_name: name, is_active: true, membership_status: "active",
  membership_source: null, roles: ["viewer"], role_ids: [], created_at: iso(-30 * DAY), ...over,
});

function makeBackend(initial: Partial<State> = {}) {
  const state: State = {
    permissions: ["incident:read", "tenancy:manage", "audit:read"],
    overview: baseOverview(),
    tokens: [token()],
    identities: [],
    pending: [],
    members: [member(ADA, "Ada"), member(BOB, "Bob")],
    audit: [],
    groups: [],
    ...initial,
  };
  // The fake backend echoes request bodies of many shapes; the assertions index into them directly.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const calls: Array<{ method: string; path: string; query: string; body: any }> = [];
  const json = (route: Route, status: number, body: unknown) =>
    route.fulfill({ status, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(body) });
  const claims = () => Buffer.from(JSON.stringify({ sub: ME, organization_id: ORG, exp: 4102444800 })).toString("base64url");
  const fail = (route: Route, status: number) => json(route, status, { error_code: status === 403 ? "auth.permission_denied" : "internal_error", message: "denied or failed", detail: null });
  const answer = (route: Route, value: unknown) => (typeof value === "number" ? fail(route, value) : json(route, 200, value));

  async function handle(route: Route) {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname;
    const method = req.method();
    if (method === "OPTIONS") {
      return route.fulfill({
        status: 204,
        headers: { "access-control-allow-origin": "*", "access-control-allow-headers": "*", "access-control-allow-methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS" },
      });
    }
    const body = req.postData() ? JSON.parse(req.postData()!) : undefined;
    calls.push({ method, path, query: url.search, body });

    if (path === "/auth/refresh") return json(route, 200, { access_token: `h.${claims()}.s`, refresh_token: "rt2", token_type: "bearer", expires_in: 3600 });
    if (path === "/auth/me") return json(route, 200, { id: ME, email: "me@example.test", display_name: "Me", is_active: true, roles: ["admin"], permissions: state.permissions });
    if (path === "/auth/organizations") return json(route, 200, { organizations: [{ id: ORG, name: "Acme", slug: "acme" }] });
    if (path === "/organizations") return json(route, 200, [{ id: ORG, name: "Acme", slug: "acme" }]);
    if (path === `/organizations/${ORG}/members` && method === "GET") return json(route, 200, state.members);
    if (path === `/organizations/${ORG}/invitations`) return json(route, 200, []);

    const memberAction = path.match(new RegExp(`^/organizations/${ORG}/members/([^/]+)/(deactivate|reactivate)$`));
    if (memberAction) {
      if (state.memberActionError) return json(route, state.memberActionError.status, { ...state.memberActionError, detail: null });
      const target = state.members.find((m) => m.id === memberAction[1]);
      if (target) {
        target.membership_status = memberAction[2] === "deactivate" ? "deactivated" : "active";
        target.membership_source = memberAction[2] === "deactivate" ? "admin" : null;
      }
      return json(route, 200, { user_id: memberAction[1], organization_id: ORG, status: target?.membership_status, source: "admin", reason: body.reason, member_epoch: 1 });
    }

    if (path === `/organizations/${ORG}/audit`) return json(route, 200, state.audit);
    if (path === `/organizations/${ORG}/roles` && method === "GET") {
      return json(route, 200, ["viewer", "engineer", "admin"].map((name) => ({ id: `role-${name}`, name, description: null, is_builtin: name === "admin", permissions: [], member_count: 1 })));
    }

    const scim = path.match(new RegExp(`^/organizations/${ORG}/scim(?:/(.*))?$`));
    if (scim) {
      const rest = scim[1] ?? "";
      if (rest === "" && method === "GET") return answer(route, state.overview);
      if (rest === "" && method === "PUT") {
        if (typeof state.overview !== "number") state.overview = { ...state.overview, ...body };
        return json(route, 200, state.overview);
      }
      if (rest === "tokens" && method === "GET") return answer(route, state.tokens);
      if (rest === "tokens" && method === "POST") {
        if (state.tokenCreateError) return json(route, state.tokenCreateError.status, { ...state.tokenCreateError, detail: null });
        const created = token({ id: "t-new", name: body.name, prefix: "scim_NewToken1" });
        if (Array.isArray(state.tokens)) state.tokens.unshift(created);
        return json(route, 201, { token: created, raw_token: "scim_RAW_SECRET_TOKEN_VALUE_123" });
      }
      const rotate = rest.match(/^tokens\/([^/]+)\/rotate$/);
      if (rotate) {
        const created = token({ id: "t-rot", name: "Entra ID", prefix: "scim_Rotated01" });
        if (Array.isArray(state.tokens)) state.tokens.unshift(created);
        return json(route, 201, { token: created, raw_token: "scim_ROTATED_SECRET_VALUE_456" });
      }
      const revoke = rest.match(/^tokens\/([^/]+)\/revoke$/);
      if (revoke) {
        if (Array.isArray(state.tokens)) state.tokens = state.tokens.map((t) => (t.id === revoke[1] ? { ...t, status: "revoked", revoked_at: iso(0) } : t));
        return json(route, 200, token({ id: revoke[1], status: "revoked" }));
      }
      if (rest.startsWith("identities") && method === "GET") return json(route, 200, { items: state.identities, total: state.identities.length, offset: 0, limit: 50 });
      const link = rest.match(/^identities\/([^/]+)\/link$/);
      if (link) {
        state.identities = state.identities.filter((i) => i.id !== link[1]);
        return json(route, 200, { id: link[1], user_name: "x", linked_user_id: body.user_id, linked_via: "admin", active: true, last_seen_at: iso(0), candidates: [] });
      }
      if (rest === "groups" && method === "GET") return json(route, 200, state.groups);
      const groupRoles = rest.match(/^groups\/([^/]+)\/roles$/);
      if (groupRoles && method === "PUT") {
        if (state.groupRolesError) return json(route, state.groupRolesError.status, { ...state.groupRolesError, detail: null });
        const target = state.groups.find((g) => g.id === groupRoles[1]);
        if (target) target.roles = (body.role_ids as string[]).map((id) => ({ id, name: id.replace("role-", "") }));
        return json(route, 200, target);
      }
      if (rest === "pending" && method === "GET") return json(route, 200, state.pending);
      const decide = rest.match(/^pending\/([^/]+)\/(approve|reject)$/);
      if (decide) {
        state.pending = state.pending.filter((p) => p.id !== decide[1]);
        return json(route, 200, { id: decide[1], user_id: ADA, user_name: "ada@example.test", reason: "r", status: decide[2] === "approve" ? "approved" : "rejected", created_at: iso(-1000), decided_at: iso(0) });
      }
    }
    return json(route, 200, []);
  }

  return {
    state,
    calls,
    handle,
    callsTo: (path: string, method?: string) => calls.filter((c) => c.path === path && (!method || c.method === method)),
    scimCalls: () => calls.filter((c) => c.path.includes("/scim")),
  };
}

async function open(page: Page, initial: Partial<State> = {}) {
  const backend = makeBackend(initial);
  await page.addInitScript(() => localStorage.setItem("ekip.refresh_token", "rt1"));
  await page.route(`${API}/**`, backend.handle);
  return backend;
}

const PAGE = "/settings/provisioning";

// ======================================================================= access

test.describe("Access", () => {
  test("the Provisioning tab is shown with tenancy:manage and hidden without it", async ({ page }) => {
    await open(page);
    await page.goto("/settings/organization");
    await expect(page.getByRole("link", { name: "Provisioning" })).toBeVisible();
    await page.unroute(`${API}/**`);
    await open(page, { permissions: ["incident:read"] });
    await page.goto("/settings/organization");
    await expect(page.getByRole("link", { name: "Users" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Provisioning" })).toHaveCount(0);
  });

  test("a direct visit without tenancy:manage is denied and calls no SCIM endpoint", async ({ page }) => {
    const backend = await open(page, { permissions: ["incident:read"] });
    await page.goto(PAGE);
    await expect(page.getByText("You don't have access to provisioning settings")).toBeVisible();
    expect(backend.scimCalls()).toHaveLength(0);
  });

  test("a 403 from the server is shown as the same denial", async ({ page }) => {
    await open(page, { overview: 403 });
    await page.goto(PAGE);
    await expect(page.getByText("You don't have access to provisioning settings")).toBeVisible();
  });
});

// ======================================================================= overview and health

test.describe("Overview and health", () => {
  test("shows the endpoint, the switch, the health and the last request times", async ({ page }) => {
    await open(page);
    await page.goto(PAGE);
    await expect(page.getByLabel("SCIM base URL", { exact: true })).toHaveValue("https://api.example.test/scim/v2");
    await expect(page.getByLabel("Enable SCIM provisioning")).toBeChecked();
    await expect(page.getByTestId("scim-health-level")).toHaveAttribute("data-level", "ok");
    await expect(page.getByTestId("scim-last-error")).toHaveText("none");
    await expect(page.getByText("this organization only")).toBeVisible();
    await expect(page.getByText("EKIP does not create accounts from SCIM")).toBeVisible();
    await expect(page.getByTestId("scim-health-warning")).toHaveCount(0);
  });

  test("the switch sends the new value", async ({ page }) => {
    const backend = await open(page);
    await page.goto(PAGE);
    await page.getByLabel("Enable SCIM provisioning").click(); // controlled: it flips once the server has answered
    await expect.poll(() => backend.callsTo(`/organizations/${ORG}/scim`, "PUT").length).toBe(1);
    expect(backend.callsTo(`/organizations/${ORG}/scim`, "PUT")[0].body).toEqual({ enabled: false });
    await expect(page.getByTestId("scim-health-level")).toHaveAttribute("data-level", "off");
  });

  test("a deployment-wide kill switch is explained", async ({ page }) => {
    await open(page, { overview: { ...baseOverview(), platform_enabled: false } });
    await page.goto(PAGE);
    await expect(page.getByTestId("scim-platform-off")).toContainText("switched off for this deployment");
    await expect(page.getByTestId("scim-health-level")).toHaveAttribute("data-level", "off");
  });

  for (const [name, overview, level, text] of [
    ["no request yet", { ...baseOverview(), last_request_at: null, last_success_at: null }, "never", "No request has been received yet"],
    ["no active token", { ...baseOverview(), active_tokens: 0 }, "no_token", "cannot authenticate"],
    ["stale synchronization", { ...baseOverview(), last_success_at: iso(-3 * DAY), last_request_at: iso(-3 * DAY) }, "stale", "No successful sync for over 48 hours"],
    ["a failing connection", { ...baseOverview(), last_success_at: iso(-3 * 3600_000), last_error_code: "scim.last_admin", last_error_at: iso(-60_000) }, "error", "failed after the last successful"],
  ] as const) {
    test(`warns clearly for ${name}`, async ({ page }) => {
      await open(page, { overview: overview as Record<string, unknown> });
      await page.goto(PAGE);
      await expect(page.getByTestId("scim-health-level")).toHaveAttribute("data-level", level);
      await expect(page.getByTestId("scim-health")).toContainText(text);
      if (level !== "never") {
        await expect(page.getByTestId("scim-health-warning")).toContainText("keep their EKIP access until synchronization works again");
      }
    });
  }

  test("an error code is shown with its time", async ({ page }) => {
    await open(page, { overview: { ...baseOverview(), last_error_code: "scim.deactivation_held", last_error_at: iso(-120_000) } });
    await page.goto(PAGE);
    await expect(page.getByTestId("scim-last-error")).toContainText("scim.deactivation_held");
  });

  test("a failed overview request shows an error with a working Retry", async ({ page }) => {
    const backend = await open(page, { overview: 500 });
    await page.goto(PAGE);
    await expect(page.getByText("Could not load provisioning settings")).toBeVisible();
    backend.state.overview = baseOverview();
    await page.getByRole("button", { name: "Retry" }).click();
    await expect(page.getByTestId("provisioning-page")).toBeVisible();
  });
});

// ======================================================================= tokens

test.describe("Tokens", () => {
  test("lists tokens by prefix only, never a secret", async ({ page }) => {
    await open(page);
    await page.goto(PAGE);
    const row = page.getByTestId("scim-token-t-1");
    await expect(row).toContainText("Entra ID");
    await expect(row).toContainText("scim_AbCdEfGh…");
    await expect(row).toContainText("active");
    await expect(page.getByTestId("scim-tokens")).not.toContainText("RAW_SECRET");
  });

  test("a new token is shown once, in a modal, and exists nowhere afterwards", async ({ page }) => {
    const backend = await open(page);
    await page.goto(PAGE);
    await page.getByRole("button", { name: "Create token" }).first().click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByRole("button", { name: "Create token" })).toBeDisabled(); // a name is required
    await dialog.getByLabel("Name").fill("Okta production");
    await dialog.getByLabel("Expires after").selectOption("180");
    await dialog.getByRole("button", { name: "Create token" }).click();

    const secret = page.getByTestId("scim-token-secret");
    await expect(page.getByRole("dialog")).toContainText("only time it is shown");
    await expect(secret.getByLabel("SCIM token", { exact: true })).toHaveValue("scim_RAW_SECRET_TOKEN_VALUE_123");
    expect(backend.callsTo(`/organizations/${ORG}/scim/tokens`, "POST")[0].body).toEqual({ name: "Okta production", ttl_days: 180 });

    await page.getByRole("button", { name: "I have stored it" }).click();
    await expect(page.getByTestId("scim-token-secret")).toHaveCount(0);
    expect(await page.content()).not.toContain("RAW_SECRET_TOKEN_VALUE");
    const storage = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }) + document.cookie);
    expect(storage).not.toContain("RAW_SECRET");
    await expect(page.getByTestId("scim-tokens")).toContainText("scim_NewToken1…");
    await page.reload();
    expect(await page.content()).not.toContain("RAW_SECRET_TOKEN_VALUE");
  });

  test("rotating shows the successor once and explains the overlap", async ({ page }) => {
    await open(page);
    await page.goto(PAGE);
    await page.getByRole("button", { name: "Rotate" }).click();
    await expect(page.getByRole("dialog")).toContainText("keeps working for 24 hours");
    await page.getByRole("dialog").getByRole("button", { name: "Rotate token" }).click();
    await expect(page.getByTestId("scim-token-secret").getByLabel("SCIM token", { exact: true })).toHaveValue("scim_ROTATED_SECRET_VALUE_456");
    await page.getByRole("button", { name: "I have stored it" }).click();
    expect(await page.content()).not.toContain("ROTATED_SECRET");
  });

  test("revoking asks for confirmation first", async ({ page }) => {
    const backend = await open(page);
    await page.goto(PAGE);
    await page.getByRole("button", { name: "Revoke", exact: true }).click();
    await expect(page.getByRole("dialog")).toContainText("stops working immediately");
    await page.getByRole("dialog").getByRole("button", { name: "Cancel" }).click();
    expect(backend.callsTo(`/organizations/${ORG}/scim/tokens/t-1/revoke`)).toHaveLength(0);
    await page.getByRole("button", { name: "Revoke", exact: true }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Revoke token" }).click();
    await expect(page.getByTestId("scim-token-t-1")).toContainText("revoked");
    await expect(page.getByTestId("token-warning")).toContainText("Every token has expired or been revoked");
  });

  test("warns when a token is about to expire", async ({ page }) => {
    await open(page, { tokens: [token({ expires_at: iso(5 * DAY) })] });
    await page.goto(PAGE);
    await expect(page.getByTestId("token-warning")).toContainText('"Entra ID" expires in 5 days');
  });

  test("warns when every token has expired", async ({ page }) => {
    await open(page, { tokens: [token({ status: "expired", expires_at: iso(-DAY) })], overview: { ...baseOverview(), active_tokens: 0 } });
    await page.goto(PAGE);
    await expect(page.getByTestId("token-warning")).toContainText("Every token has expired");
    await expect(page.getByTestId("scim-token-t-1").getByRole("button", { name: "Revoke" })).toHaveCount(0);
  });

  test("an empty list invites you to create the first token", async ({ page }) => {
    await open(page, { tokens: [] });
    await page.goto(PAGE);
    await expect(page.getByText("No tokens yet")).toBeVisible();
  });

  test("the maximum-tokens error is explained", async ({ page }) => {
    await open(page, { tokenCreateError: { status: 409, error_code: "scim.too_many_tokens", message: "x" } });
    await page.goto(PAGE);
    await page.getByRole("button", { name: "Create token" }).first().click();
    await page.getByRole("dialog").getByLabel("Name").fill("Another");
    await page.getByRole("dialog").getByRole("button", { name: "Create token" }).click();
    await expect(page.getByText("maximum number of active tokens")).toBeVisible();
    await expect(page.getByTestId("scim-token-secret")).toHaveCount(0);
  });

  test("a failed token list is reported with Retry", async ({ page }) => {
    await open(page, { tokens: 500 });
    await page.goto(PAGE);
    await expect(page.getByText("Could not load tokens")).toBeVisible();
  });
});

// ======================================================================= held deactivations, identities, safety

test.describe("Approvals, linking and limits", () => {
  const held = { id: "p-1", user_id: ADA, user_name: "ada@example.test", reason: "Deactivated in the identity provider", status: "pending", created_at: iso(-60_000), decided_at: null };

  test("held deactivations are listed and need an explicit decision", async ({ page }) => {
    const backend = await open(page, { pending: [held] });
    await page.goto(PAGE);
    await expect(page.getByTestId("scim-pending")).toContainText("ada@example.test");
    await expect(page.getByText("Nothing has changed for them yet")).toBeVisible();
    await page.getByRole("button", { name: "Approve deactivation" }).first().click();
    await expect(page.getByRole("dialog")).toContainText("loses access to this organization immediately");
    await page.getByRole("dialog").getByRole("button", { name: "Approve deactivation" }).click();
    await expect.poll(() => backend.callsTo(`/organizations/${ORG}/scim/pending/p-1/approve`, "POST").length).toBe(1);
    await expect(page.getByTestId("scim-pending")).toHaveCount(0); // the card disappears when nothing is held
  });

  test("a held deactivation can be rejected", async ({ page }) => {
    const backend = await open(page, { pending: [held] });
    await page.goto(PAGE);
    await page.getByRole("button", { name: "Reject", exact: true }).click();
    await expect(page.getByRole("dialog")).toContainText("keeps their access");
    await page.getByRole("dialog").getByRole("button", { name: "Reject" }).click();
    await expect.poll(() => backend.callsTo(`/organizations/${ORG}/scim/pending/p-1/reject`, "POST").length).toBe(1);
  });

  test("an unlinked identity is only a suggestion until an administrator links it", async ({ page }) => {
    const backend = await open(page, {
      identities: [{ id: "i-1", user_name: "ada@example.test", external_id: "00u1", display_name: "Ada", active: true, linked_user_id: null, linked_via: null, last_seen_at: iso(-1000), candidates: [{ user_id: ADA, email: "ada@example.test", display_name: "Ada" }] }],
    });
    await page.goto(PAGE);
    const list = page.getByTestId("scim-identities");
    await expect(list).toContainText("possible match: ada@example.test");
    await expect(page.getByText("nothing is linked, and nobody is deactivated, until you confirm")).toBeVisible();
    await page.getByRole("button", { name: "Link to a member" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByRole("button", { name: "Link identity" })).toBeDisabled(); // a member must be chosen
    await dialog.getByLabel("Member").selectOption(ADA);
    await dialog.getByRole("button", { name: "Link identity" }).click();
    await expect.poll(() => backend.callsTo(`/organizations/${ORG}/scim/identities/i-1/link`, "POST").length).toBe(1);
    expect(backend.callsTo(`/organizations/${ORG}/scim/identities/i-1/link`)[0].body).toEqual({ user_id: ADA });
  });

  test("linking an identity the IdP reports inactive warns that it deactivates immediately", async ({ page }) => {
    await open(page, {
      identities: [{ id: "i-2", user_name: "bob@example.test", external_id: null, display_name: null, active: false, linked_user_id: null, linked_via: null, last_seen_at: iso(-1000), candidates: [] }],
    });
    await page.goto(PAGE);
    await page.getByRole("button", { name: "Link to a member" }).click();
    await expect(page.getByRole("dialog")).toContainText("deactivated immediately");
  });

  test("the safety limits are saved as entered", async ({ page }) => {
    const backend = await open(page);
    await page.goto(PAGE);
    await page.getByLabel("At least this many per window").fill("3");
    await page.getByLabel("Or this percent of active members").fill("10");
    await page.getByLabel("Window (minutes)").fill("30");
    await page.getByLabel("Re-enable settle time (seconds)").fill("120");
    await page.getByRole("button", { name: "Save safety limits" }).click();
    await expect.poll(() => backend.callsTo(`/organizations/${ORG}/scim`, "PUT").length).toBe(1);
    expect(backend.callsTo(`/organizations/${ORG}/scim`, "PUT")[0].body).toEqual({
      guard_min_absolute: 3, guard_percent: 10, guard_window_minutes: 30, reactivation_settle_seconds: 120,
    });
    await expect(page.getByText("last administrator can never be deactivated through SCIM")).toBeVisible();
  });
});

// ======================================================================= members

test.describe("Members: status and access actions", () => {
  test("shows each member's access state and who deactivated them", async ({ page }) => {
    await open(page, {
      members: [member(ADA, "Ada", { membership_status: "deactivated", membership_source: "scim" }), member(BOB, "Bob", { membership_status: "deactivated", membership_source: "admin" }), member("c0c0c0c0-0000-4000-8000-000000000003", "Cy")],
    });
    await page.goto("/settings/users");
    await expect(page.getByTestId(`member-status-${ADA}`)).toContainText("Deactivated");
    await expect(page.getByTestId(`member-status-${ADA}`)).toContainText("By your identity provider");
    await expect(page.getByTestId(`member-status-${BOB}`)).toContainText("By an administrator");
    await expect(page.getByTestId("member-status-c0c0c0c0-0000-4000-8000-000000000003")).toHaveAttribute("data-status", "active");
  });

  test("deactivating needs a reason and an explicit confirmation, then sends both", async ({ page }) => {
    const backend = await open(page);
    await page.goto("/settings/users");
    await page.getByRole("row", { name: /Ada/ }).getByRole("button", { name: "Deactivate" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("signed out of this organization immediately");
    await expect(dialog).toContainText("not affected");
    await dialog.getByRole("button", { name: "Deactivate access" }).click();
    await expect(dialog.getByText("A reason is required.")).toBeVisible();
    expect(backend.calls.filter((c) => c.path.endsWith("/deactivate"))).toHaveLength(0);
    await dialog.getByLabel(/^Reason/).fill("Left the company");
    await dialog.getByRole("button", { name: "Deactivate access" }).click();
    await expect(dialog.getByText("Confirm that you understand the effect.")).toBeVisible();
    expect(backend.calls.filter((c) => c.path.endsWith("/deactivate"))).toHaveLength(0);
    await dialog.getByRole("checkbox").check();
    await dialog.getByRole("button", { name: "Deactivate access" }).dblclick(); // a double click is one request
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect(backend.calls.filter((c) => c.path.endsWith("/deactivate"))).toHaveLength(1);
    expect(backend.callsTo(`/organizations/${ORG}/members/${ADA}/deactivate`)[0].body).toEqual({ reason: "Left the company" });
    await expect(page.getByTestId(`member-status-${ADA}`)).toHaveAttribute("data-status", "deactivated");
  });

  test("reactivating needs a reason, no confirmation, and says the person signs in again", async ({ page }) => {
    const backend = await open(page, { members: [member(ADA, "Ada", { membership_status: "deactivated", membership_source: "admin" })] });
    await page.goto("/settings/users");
    await page.getByRole("button", { name: "Reactivate" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("must sign in again");
    await expect(dialog.getByRole("checkbox")).toHaveCount(0);
    await dialog.getByRole("button", { name: "Reactivate access" }).click();
    await expect(dialog.getByText("A reason is required.")).toBeVisible();
    await dialog.getByLabel(/^Reason/).fill("Rehired");
    await dialog.getByRole("button", { name: "Reactivate access" }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect(backend.callsTo(`/organizations/${ORG}/members/${ADA}/reactivate`)[0].body).toEqual({ reason: "Rehired" });
  });

  test("the last-administrator refusal is explained and nothing changes", async ({ page }) => {
    await open(page, { memberActionError: { status: 409, error_code: "org.last_admin", message: "x" } });
    await page.goto("/settings/users");
    await page.getByRole("row", { name: /Ada/ }).getByRole("button", { name: "Deactivate" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel(/^Reason/).fill("r");
    await dialog.getByRole("checkbox").check();
    await dialog.getByRole("button", { name: "Deactivate access" }).click();
    await expect(dialog.getByTestId("member-access-error")).toContainText("without an administrator");
    await expect(dialog).toBeVisible(); // stays open so the reason is not lost
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(page.getByTestId(`member-status-${ADA}`)).toHaveAttribute("data-status", "active");
  });

  test("an authorization error is explained", async ({ page }) => {
    await open(page, { memberActionError: { status: 403, error_code: "auth.permission_denied", message: "x" } });
    await page.goto("/settings/users");
    await page.getByRole("row", { name: /Bob/ }).getByRole("button", { name: "Deactivate" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel(/^Reason/).fill("r");
    await dialog.getByRole("checkbox").check();
    await dialog.getByRole("button", { name: "Deactivate access" }).click();
    await expect(dialog.getByTestId("member-access-error")).toContainText("tenancy:manage");
  });

  test("without tenancy:manage there are no access actions", async ({ page }) => {
    await open(page, { permissions: ["incident:read"] });
    await page.goto("/settings/users");
    await expect(page.getByTestId(`member-status-${ADA}`)).toBeVisible();
    await expect(page.getByRole("button", { name: "Deactivate" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Reactivate" })).toHaveCount(0);
  });
});

// ======================================================================= audit log

test.describe("Audit log", () => {
  const entries = [
    { id: "a1", organization_id: ORG, actor: "agent:scim:scim_AbCdEfGh", action: "member.deactivate", resource_type: "user", resource_id: ADA, event_metadata: { source: "scim", reason: "Deactivated in the identity provider", member_epoch: 1 }, occurred_at: iso(-60_000) },
    { id: "a2", organization_id: ORG, actor: `user:${ME}`, action: "scim.token_created", resource_type: "scim_token", resource_id: null, event_metadata: { name: "Entra ID", prefix: "scim_AbCdEfGh" }, occurred_at: iso(-120_000) },
    { id: "a3", organization_id: ORG, actor: "agent:scim:scim_AbCdEfGh", action: "scim.deactivation_refused", resource_type: "user", resource_id: BOB, event_metadata: { reason: "last_admin" }, occurred_at: iso(-180_000) },
    { id: "a4", organization_id: ORG, actor: `user:${ME}`, action: "incident.create", resource_type: "incident", resource_id: null, event_metadata: null, occurred_at: iso(-240_000) },
  ];

  test("events are described in plain language, with the reason, and unknown ones as recorded", async ({ page }) => {
    await open(page, { audit: entries });
    await page.goto("/audit");
    await expect(page.getByText("Member access deactivated by the identity provider")).toBeVisible();
    await expect(page.getByText("Reason: Deactivated in the identity provider")).toBeVisible();
    await expect(page.getByText("SCIM token created")).toBeVisible();
    await expect(page.getByText("Deactivation refused (last administrator)")).toBeVisible();
    await expect(page.getByText("incident.create")).toBeVisible();
    const html = (await page.content()).toLowerCase();
    expect(html).not.toContain("internal_note");
    expect(html).not.toContain("token_hash");
  });

  test("the event-type filter is sent to the server as an action prefix", async ({ page }) => {
    const backend = await open(page, { audit: entries });
    await page.goto("/audit");
    await expect(page.getByLabel("Filter by event type")).toHaveValue("");
    await page.getByLabel("Filter by event type").selectOption("scim.");
    await expect.poll(() => backend.callsTo(`/organizations/${ORG}/audit`).some((c) => c.query.includes("action_prefix=scim."))).toBe(true);
    await page.getByLabel("Filter by event type").selectOption("member.");
    await expect.poll(() => backend.callsTo(`/organizations/${ORG}/audit`).some((c) => c.query.includes("action_prefix=member."))).toBe(true);
    await page.getByLabel("Filter by event type").selectOption("");
    // back to "all events": nothing is filtered (the unfiltered list comes from the query cache), and the very
    // first request, made before any filter was chosen, carried no prefix
    expect(backend.callsTo(`/organizations/${ORG}/audit`)[0].query).not.toContain("action_prefix");
    await expect(page.getByText("incident.create")).toBeVisible();
  });

  test("the audit log is still denied without audit:read", async ({ page }) => {
    await open(page, { permissions: ["incident:read"] });
    await page.goto("/audit");
    await expect(page.getByRole("link", { name: "Audit Log" })).toHaveCount(0);
  });
});

// ======================================================================= groups and roles

const group = (over: Record<string, unknown> = {}) => ({
  id: "g-1", display_name: "Engineering", external_id: null, member_count: 12, roles: [], updated_at: iso(-1000), ...over,
});

test.describe("Groups and roles", () => {
  test("a group grants nothing until roles are chosen, and says so", async ({ page }) => {
    await open(page, { groups: [group(), group({ id: "g-2", display_name: "Ops", member_count: 1, roles: [{ id: "role-viewer", name: "viewer" }] })] });
    await page.goto(PAGE);
    const list = page.getByTestId("scim-groups");
    await expect(list).toContainText("Engineering");
    await expect(list).toContainText("12 members · grants no roles yet");
    await expect(list).toContainText("1 member · grants viewer");
    await expect(page.getByText("grants nothing until you choose its roles")).toBeVisible();
    await expect(page.getByText("Roles you assigned by hand are never removed")).toBeVisible();
  });

  test("the card is not shown when the identity provider has pushed no groups", async ({ page }) => {
    await open(page, { groups: [] });
    await page.goto(PAGE);
    await expect(page.getByText("SCIM provisioning").first()).toBeVisible();
    await expect(page.getByTestId("scim-groups")).toHaveCount(0);
    await expect(page.getByText("Groups and roles")).toHaveCount(0);
  });

  test("choosing roles sends exactly the selected role ids", async ({ page }) => {
    const backend = await open(page, { groups: [group({ roles: [{ id: "role-viewer", name: "viewer" }] })] });
    await page.goto(PAGE);
    await page.getByRole("button", { name: "Choose roles" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByLabel("viewer")).toBeChecked(); // current mapping preselected
    await expect(dialog.getByLabel("engineer")).not.toBeChecked();
    await dialog.getByLabel("engineer").click();
    await dialog.getByLabel("viewer").click();
    await expect(dialog).toContainText("roles you remove here are withdrawn from members who held them only through this group");
    await dialog.getByRole("button", { name: "Save roles" }).click();
    await expect.poll(() => backend.callsTo(`/organizations/${ORG}/scim/groups/g-1/roles`, "PUT").length).toBe(1);
    expect(backend.callsTo(`/organizations/${ORG}/scim/groups/g-1/roles`)[0].body).toEqual({ role_ids: ["role-engineer"] });
    await expect(page.getByTestId("scim-groups")).toContainText("grants engineer");
  });

  test("a privilege-escalation refusal is explained and the dialog stays open", async ({ page }) => {
    await open(page, {
      groups: [group()],
      groupRolesError: { status: 403, error_code: "role.permission_escalation", message: "x" },
    });
    await page.goto(PAGE);
    await page.getByRole("button", { name: "Choose roles" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("admin").click();
    await dialog.getByRole("button", { name: "Save roles" }).click();
    await expect(page.getByText("You cannot map a role that has permissions you do not hold yourself.")).toBeVisible();
    await expect(dialog).toBeVisible();
  });

  test("group events are described in plain language in the audit log", async ({ page }) => {
    await open(page, {
      audit: [
        { id: "g1", organization_id: ORG, action: "scim.role_granted", actor: "agent:scim:x", resource_type: "user", resource_id: ADA, event_metadata: { via: "group" }, occurred_at: iso(-1000) },
        { id: "g2", organization_id: ORG, action: "scim.role_revoke_refused", actor: "agent:scim:x", resource_type: "user", resource_id: ADA, event_metadata: { reason: "last_admin" }, occurred_at: iso(-2000) },
        { id: "g3", organization_id: ORG, action: "scim.group_roles_updated", actor: `user:${ME}`, resource_type: "scim_group", resource_id: null, event_metadata: {}, occurred_at: iso(-3000) },
      ],
    });
    await page.goto("/audit");
    await expect(page.getByText("Role granted through a group")).toBeVisible();
    await expect(page.getByText("Role withdrawal refused (last administrator)")).toBeVisible();
    await expect(page.getByText("Group role mapping changed")).toBeVisible();
  });
});
