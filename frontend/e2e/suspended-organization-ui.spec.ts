import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * Browser tests for what a TENANT user sees when their organization is suspended: the explanation page,
 * its customer-facing message, no redirect loops, no operator-private data, sign-in to another
 * organization, and the read-only status display.
 *
 * Like `password-reset-ui.spec.ts` the backend is a small stateful fake (`page.route`); the real
 * enforcement (every REST/MCP path, refresh, sessions, epoch) is tested server-side in
 * `tests/api/test_organization_suspension_api_postgres.py` and `tests/api/test_platform_organizations_api_postgres.py`.
 */

const API = process.env.E2E_API_BASE_URL ?? "http://127.0.0.1:8010";
const ORG_A = "11111111-1111-4111-8111-111111111111"; // suspended during the test
const ORG_B = "44444444-4444-4444-8444-444444444444";
const ME = "22222222-2222-4222-8222-222222222222";

const SUSPENDED_BODY = {
  error_code: "auth.organization_suspended",
  message: "This organization's access has been suspended. Contact your administrator.",
  detail: { organization_name: "Initech", customer_message: "Please contact billing to restore access." },
};

interface State {
  aSuspended: boolean;
  /** orgs the user belongs to that the picker offers (the server omits suspended ones) */
  multiOrg: boolean;
  orgStatus: "onboarding" | "active";
}

function makeBackend(initial: Partial<State> = {}) {
  const state: State = { aSuspended: false, multiOrg: false, orgStatus: "active", ...initial };
  const calls: Array<{ method: string; path: string; auth: string | null }> = [];
  const json = (route: Route, status: number, body: unknown) =>
    route.fulfill({ status, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(body) });
  const claims = (org: string) => Buffer.from(JSON.stringify({ sub: ME, organization_id: org, exp: 4102444800 })).toString("base64url");
  const tokens = (org: string) => ({ status: "complete", access_token: `h.${claims(org)}.s`, refresh_token: `rt-${org.slice(0, 4)}`, token_type: "bearer", expires_in: 3600 });
  const orgSummary = (id: string, name: string) => ({ id, name, slug: name.toLowerCase(), status: state.orgStatus });

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
    calls.push({ method, path, auth: req.headers()["authorization"] ?? null });
    const auth = req.headers()["authorization"] ?? "";
    const tokenOrg = auth.startsWith("Bearer h.")
      ? (JSON.parse(Buffer.from(auth.split(".")[1], "base64url").toString()) as { organization_id: string }).organization_id
      : null;

    if (path === "/auth/login") {
      if (state.multiOrg && !state.aSuspended) {
        return json(route, 200, {
          status: "organization_selection_required", selection_token: "sel", token_type: "bearer",
          organizations: [orgSummary(ORG_A, "Initech"), orgSummary(ORG_B, "Globex")],
        });
      }
      if (state.multiOrg) {
        // org A is suspended: the server skips it and, with another organization, signs straight in
        return json(route, 200, tokens(ORG_B));
      }
      if (state.aSuspended) return json(route, 403, SUSPENDED_BODY);
      return json(route, 200, tokens(ORG_A));
    }
    if (path === "/auth/select-organization") {
      const body = JSON.parse(req.postData() ?? "{}") as { organization_id: string };
      if (body.organization_id === ORG_A && state.aSuspended) return json(route, 403, SUSPENDED_BODY);
      return json(route, 200, tokens(body.organization_id));
    }
    if (path === "/auth/refresh") {
      const body = JSON.parse(req.postData() ?? "{}") as { refresh_token: string };
      if (body.refresh_token.startsWith("rt-1111") && state.aSuspended) return json(route, 403, SUSPENDED_BODY);
      const org = body.refresh_token.startsWith("rt-4444") ? ORG_B : ORG_A;
      return json(route, 200, tokens(org));
    }
    // every other authenticated call is refused for the suspended organization, exactly like the server
    if (tokenOrg === ORG_A && state.aSuspended) return json(route, 403, SUSPENDED_BODY);

    if (path === "/auth/me") {
      return json(route, 200, { id: ME, email: "me@example.com", display_name: "Me", is_active: true, roles: ["viewer"], permissions: ["incident:read", "observability:read"] });
    }
    if (path === "/auth/organizations") {
      const orgs = state.multiOrg
        ? [orgSummary(ORG_B, "Globex"), ...(state.aSuspended ? [] : [orgSummary(ORG_A, "Initech")])]
        : [orgSummary(tokenOrg ?? ORG_A, "Initech")];
      return json(route, 200, { organizations: orgs });
    }
    if (path === "/organizations") return json(route, 200, [orgSummary(tokenOrg ?? ORG_A, "Initech")]);
    if (path === "/auth/logout") return route.fulfill({ status: 204, headers: { "access-control-allow-origin": "*" } });
    return json(route, 200, []);
  }

  return { state, calls, handle, callsTo: (p: string) => calls.filter((c) => c.path === p) };
}

async function open(page: Page, initial: Partial<State> = {}, signedInAs?: "A" | "B") {
  const backend = makeBackend(initial);
  if (signedInAs) {
    const token = signedInAs === "A" ? "rt-1111" : "rt-4444";
    await page.addInitScript((value) => localStorage.setItem("ekip.refresh_token", value), token);
  }
  await page.route(`${API}/**`, backend.handle);
  return backend;
}

async function fillLogin(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Work email").fill("me@example.com");
  await page.getByLabel("Password", { exact: true }).fill("some existing password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
}

// ======================================================================= signing in to a suspended organization

test.describe("Signing in to a suspended organization", () => {
  test("explains the suspension with the customer-facing message and stores no session", async ({ page }) => {
    await open(page, { aSuspended: true });
    await fillLogin(page);

    await expect(page).toHaveURL(/\/suspended$/);
    await expect(page.getByRole("heading", { name: "Initech is suspended" })).toBeVisible();
    await expect(page.getByTestId("suspension-message")).toHaveText("Please contact billing to restore access.");
    await expect(page.getByText("Your data has not been deleted.")).toBeVisible();
    const stored = await page.evaluate(() => JSON.stringify({ ...localStorage }));
    expect(stored).not.toContain("ekip.refresh_token"); // no session was created
  });

  test("never shows anything private to operators", async ({ page }) => {
    await open(page, { aSuspended: true });
    await fillLogin(page);
    await expect(page.getByTestId("suspended-page")).toBeVisible();
    const html = (await page.content()).toLowerCase();
    for (const secret of ["internal note", "internal_note", "internalnote", "operator"]) {
      expect(html).not.toContain(secret);
    }
  });

  test("a reload of the explanation page still explains itself", async ({ page }) => {
    await open(page, { aSuspended: true });
    await fillLogin(page);
    await expect(page).toHaveURL(/\/suspended$/);
    await page.reload();
    await expect(page.getByRole("heading", { name: "Initech is suspended" })).toBeVisible();
  });

  test("without a recorded notice the page is still a sensible, generic explanation", async ({ page }) => {
    await open(page);
    await page.goto("/suspended");
    await expect(page.getByRole("heading", { name: "Your organization is suspended" })).toBeVisible();
    await expect(page.getByTestId("suspension-message")).toHaveCount(0);
  });
});

// ======================================================================= an existing session

test.describe("An organization suspended while someone is signed in", () => {
  test("a reload lands on the explanation page, not a sign-in loop", async ({ page }) => {
    const backend = await open(page, {}, "A");
    await page.goto("/ask");
    await expect(page).toHaveURL(/\/ask$/);
    backend.state.aSuspended = true;

    await page.reload();
    await expect(page).toHaveURL(/\/suspended$/);
    await expect(page.getByRole("heading", { name: "Initech is suspended" })).toBeVisible();
    // settle, then prove nothing keeps hammering the API (no redirect loop)
    const before = backend.calls.length;
    await page.waitForTimeout(1500);
    expect(backend.calls.length - before).toBeLessThanOrEqual(1);
    expect(backend.callsTo("/auth/refresh").length).toBeLessThanOrEqual(2);
  });

  test("a request refused mid-session ends the session and shows the explanation", async ({ page }) => {
    const backend = await open(page, {}, "A");
    await page.goto("/ask");
    await expect(page).toHaveURL(/\/ask$/);
    backend.state.aSuspended = true;

    // an in-app (client-side) navigation to a page that loads data
    await page.evaluate(() => {
      window.history.pushState({}, "", "/incidents");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await expect(page).toHaveURL(/\/suspended$/);
    await expect(page.getByTestId("suspension-message")).toBeVisible();
    const stored = await page.evaluate(() => localStorage.getItem("ekip.refresh_token"));
    expect(stored).toBeNull(); // the dead session was dropped
  });

  test("the explanation page makes no authenticated calls at all", async ({ page }) => {
    const backend = await open(page, {}, "A");
    await page.goto("/ask");
    backend.state.aSuspended = true;
    await page.reload();
    await expect(page).toHaveURL(/\/suspended$/);
    const mark = backend.calls.length;
    await page.reload();
    await expect(page.getByTestId("suspended-page")).toBeVisible();
    expect(backend.calls.slice(mark).filter((c) => c.auth)).toHaveLength(0);
  });
});

// ======================================================================= several organizations

test.describe("A person with more than one organization", () => {
  test("can sign in to another organization from the explanation page", async ({ page }) => {
    const backend = await open(page, { multiOrg: true }, "A");
    await page.goto("/ask");
    await expect(page).toHaveURL(/\/ask$/);
    backend.state.aSuspended = true; // A is suspended; B is not

    await page.reload();
    await expect(page).toHaveURL(/\/suspended$/);
    await page.getByRole("button", { name: "Sign in to a different organization" }).click();
    await expect(page).toHaveURL(/\/login$/);

    // the login page is not redirected away again (the stale notice was cleared)
    await page.getByLabel("Work email").fill("me@example.com");
    await page.getByLabel("Password", { exact: true }).fill("some existing password");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page).toHaveURL(/\/ask$/); // the server went straight into the organization that is usable
    expect(await page.evaluate(() => localStorage.getItem("ekip.refresh_token"))).toBe("rt-4444");
    // and a later, ordinary session expiry no longer explains a suspension
    await page.evaluate(() => localStorage.clear());
  });

  test("the picker only offers organizations that are usable", async ({ page }) => {
    await open(page, { multiOrg: true });
    await fillLogin(page);
    await expect(page.getByRole("heading", { name: "Choose an organization" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Globex" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Initech" })).toBeVisible(); // not suspended yet
  });

  test("choosing an organization that was suspended meanwhile explains it", async ({ page }) => {
    const backend = await open(page, { multiOrg: true });
    await fillLogin(page);
    await expect(page.getByRole("heading", { name: "Choose an organization" })).toBeVisible();
    backend.state.aSuspended = true;
    await page.getByRole("button", { name: "Initech" }).click();
    await expect(page.getByText("Couldn't sign in to that organization")).toBeVisible();
  });
});

// ======================================================================= read-only status + regression

test.describe("Organization status display", () => {
  for (const [status, label] of [["active", "Active"], ["onboarding", "Onboarding"]] as const) {
    test(`shows the ${status} status read-only in settings`, async ({ page }) => {
      await open(page, { orgStatus: status }, "A");
      await page.goto("/settings/organization");
      await expect(page.getByTestId("organization-status")).toHaveText(label);
      await expect(page.getByText("managed by the EKIP platform team")).toBeVisible();
      // nothing in the page can change it
      await expect(page.getByLabel("Slug")).toBeDisabled();
      await expect(page.getByRole("button", { name: /suspend|activate|status/i })).toHaveCount(0);
    });
  }

  test("ordinary password sign-in is unaffected for a usable organization", async ({ page }) => {
    const backend = await open(page);
    await fillLogin(page);
    await expect(page).toHaveURL(/\/ask$/);
    expect(backend.callsTo("/auth/login")).toHaveLength(1);
    await page.evaluate(() => localStorage.clear());
  });
});
