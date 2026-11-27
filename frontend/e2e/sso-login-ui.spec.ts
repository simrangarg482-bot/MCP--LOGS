import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * Browser tests for single sign-on: the SSO block on the login page, `/o/:slug/login`, the
 * provider round trip, the callback page and the SSO settings page.
 *
 * Like `roles-ui.spec.ts` this answers every backend call with a small stateful fake
 * (`page.route`) -- INCLUDING a stand-in "identity provider" page that simply redirects back --
 * so it needs only the Vite DEV server (React StrictMode on, which is what exercises the
 * double-effect guard) built with `VITE_USE_MOCK_DATA=false VITE_API_BASE_URL=http://127.0.0.1:8010`.
 * It proves what the UI does with the contract. The security rules themselves (single-use
 * transactions, state/nonce/PKCE, SSRF, RLS, ...) are enforced and tested server-side
 * (`tests/core/auth/test_sso_*`); no real identity provider is involved here.
 */

const API = process.env.E2E_API_BASE_URL ?? "http://127.0.0.1:8010";
const ORG = "11111111-1111-4111-8111-111111111111";
const ME = "22222222-2222-4222-8222-222222222222";

type Provider = "entra_id" | "okta" | "auth0" | "google_workspace";

interface Options {
  /** slug -> provider; anything else is "not available" */
  orgs?: Record<string, Provider>;
  /** what the stand-in provider appends when sending the browser back */
  idp?: "ok" | "cancel" | "state_mismatch" | "server_error";
  callbackError?: { status: number; error_code: string; message: string };
  beginError?: { status: number; error_code: string; message: string };
  beginDelayMs?: number;
  ssoConfig?: null | { provider: Provider; enabled: boolean };
  patchConflict?: { identity_link_count: number };
  permissions?: string[];
}

function makeBackend(options: Options) {
  const orgs = options.orgs ?? { acme: "okta" };
  const calls: Array<{ method: string; path: string; query: string; body: unknown }> = [];
  const urls: string[] = [];
  let config = options.ssoConfig === undefined ? { provider: "okta" as Provider, enabled: true } : options.ssoConfig;

  const json = (route: Route, status: number, body: unknown) =>
    route.fulfill({ status, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(body) });

  const configBody = () => ({
    id: "cfg-1", organization_id: ORG, provider: config!.provider, protocol: "oidc",
    issuer_url: "https://acme.okta.com", client_id: "client-1", client_secret_ref: "••••••••",
    enabled: config!.enabled, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  });

  async function handle(route: Route) {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname;
    const method = req.method();
    urls.push(req.url());
    if (method === "OPTIONS") {
      return route.fulfill({
        status: 204,
        headers: {
          "access-control-allow-origin": "*",
          "access-control-allow-headers": "*",
          "access-control-allow-methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
        },
      });
    }
    const body = req.postData() ? JSON.parse(req.postData()!) : undefined;
    calls.push({ method, path, query: url.search, body });

    // ---- the stand-in identity provider: a plain redirect back to the app
    if (path === "/fake-idp/authorize") {
      const redirect = url.searchParams.get("redirect_uri")!;
      const state = url.searchParams.get("state")!;
      const target = new URL(redirect);
      if (options.idp === "cancel") {
        target.searchParams.set("error", "access_denied");
        target.searchParams.set("error_description", "The user cancelled");
        target.searchParams.set("state", state);
      } else if (options.idp === "server_error") {
        target.searchParams.set("error", "server_error");
        target.searchParams.set("state", state);
      } else {
        target.searchParams.set("code", "auth-code-123");
        target.searchParams.set("state", options.idp === "state_mismatch" ? "someone-elses-state" : state);
      }
      return route.fulfill({ status: 302, headers: { location: target.toString() } });
    }

    const optionsMatch = path.match(/^\/auth\/([^/]+)\/sso-options$/);
    if (optionsMatch) {
      const provider = orgs[optionsMatch[1]];
      const labels: Record<string, string> = { entra_id: "Microsoft Entra ID", okta: "Okta", auth0: "Auth0", google_workspace: "Google Workspace" };
      return json(route, 200, provider ? { available: true, provider, provider_label: labels[provider] } : { available: false, provider: null, provider_label: null });
    }
    const beginMatch = path.match(/^\/auth\/([^/]+)\/login$/);
    if (beginMatch && method === "GET") {
      if (options.beginDelayMs) await new Promise((r) => setTimeout(r, options.beginDelayMs));
      if (options.beginError) return json(route, options.beginError.status, options.beginError);
      const redirect = url.searchParams.get("redirect_uri")!;
      return json(route, 200, {
        authorization_url: `${API}/fake-idp/authorize?redirect_uri=${encodeURIComponent(redirect)}&state=state-abc&client_id=x`,
        state: "state-abc",
        transaction_id: "txn-opaque-handle",
      });
    }
    if (path === "/auth/callback" && method === "POST") {
      if (options.callbackError) return json(route, options.callbackError.status, options.callbackError);
      const claims = Buffer.from(JSON.stringify({ sub: ME, organization_id: ORG, exp: 4102444800 })).toString("base64url");
      return json(route, 200, { status: "complete", access_token: `h.${claims}.s`, refresh_token: "rt-sso", token_type: "bearer", expires_in: 900 });
    }
    if (path === "/auth/login" && method === "POST") {
      const claims = Buffer.from(JSON.stringify({ sub: ME, organization_id: ORG, exp: 4102444800 })).toString("base64url");
      return json(route, 200, { status: "complete", access_token: `h.${claims}.s`, refresh_token: "rt-pw", token_type: "bearer", expires_in: 900 });
    }
    if (path === "/auth/me") {
      return json(route, 200, {
        id: ME, email: "me@example.com", display_name: "Me Admin", is_active: true, roles: ["admin"],
        permissions: options.permissions ?? ["tenancy:manage", "incident:read", "observability:read"],
      });
    }
    if (path === "/auth/refresh") {
      const claims = Buffer.from(JSON.stringify({ sub: ME, organization_id: ORG, exp: 4102444800 })).toString("base64url");
      return json(route, 200, { access_token: `h.${claims}.s`, refresh_token: "rt2", token_type: "bearer", expires_in: 3600 });
    }
    if (path === "/auth/organizations") return json(route, 200, { organizations: [{ id: ORG, name: "Acme", slug: "acme" }] });
    if (path === "/organizations") return json(route, 200, [{ id: ORG, name: "Acme", slug: "acme" }]);

    if (path === `/organizations/${ORG}/sso` && method === "GET") {
      if (!config) return json(route, 404, { error_code: "sso_configuration.not_found", message: "not configured" });
      return json(route, 200, configBody());
    }
    if (path === `/organizations/${ORG}/sso/configure` && method === "POST") {
      config = { provider: body.provider, enabled: body.enabled };
      return json(route, 201, configBody());
    }
    if (path === `/organizations/${ORG}/sso` && method === "PATCH") {
      if (options.patchConflict && !body.confirm_identity_reset && (body.issuer_url || body.provider)) {
        return json(route, 409, { error_code: "sso.identity_reset_required", message: "confirm", detail: options.patchConflict });
      }
      if (body.enabled !== undefined) config!.enabled = body.enabled;
      if (body.provider) config!.provider = body.provider;
      return json(route, 200, configBody());
    }
    return json(route, 200, []);
  }

  return { handle, calls, urls, callsTo: (p: string) => calls.filter((c) => c.path === p) };
}

async function open(page: Page, options: Options = {}, signedIn = false) {
  const backend = makeBackend(options);
  if (signedIn) await page.addInitScript(() => localStorage.setItem("ekip.refresh_token", "rt1"));
  await page.route(`${API}/**`, backend.handle);
  return backend;
}

// ======================================================================= login page

test.describe("Login page", () => {
  test("keeps email/password sign-in exactly as it was and adds an SSO option below it", async ({ page }) => {
    const backend = await open(page);
    await page.goto("/login");

    await expect(page.getByLabel("Work email")).toBeVisible();
    await expect(page.getByLabel("Password")).toBeVisible();
    await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeVisible();
    await expect(page.getByTestId("sso-signin")).toBeVisible();
    // no provider is advertised until an organization is named
    await expect(page.getByRole("button", { name: /Continue with/ })).toHaveCount(0);
    expect(backend.callsTo("/auth/acme/sso-options")).toHaveLength(0);

    await page.getByLabel("Work email").fill("me@example.com");
    await page.getByLabel("Password").fill("correct horse battery");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page).toHaveURL(/\/ask$/);
    expect(backend.callsTo("/auth/login")).toHaveLength(1);
    expect(backend.callsTo("/auth/callback")).toHaveLength(0);
  });

  for (const [provider, label] of [
    ["entra_id", "Continue with Microsoft"],
    ["okta", "Continue with Okta"],
    ["google_workspace", "Continue with Google"],
    ["auth0", "Continue with Auth0"],
  ] as const) {
    test(`offers only the configured provider: ${provider}`, async ({ page }) => {
      await open(page, { orgs: { corp: provider } });
      await page.goto("/login");
      await page.getByLabel("Organization").fill("  Corp ");
      await page.getByRole("button", { name: "Continue", exact: true }).click();

      await expect(page.getByRole("button", { name: label })).toBeVisible();
      await expect(page.getByRole("button", { name: /Continue with/ })).toHaveCount(1);
    });
  }

  test("an unknown organization and one without SSO look exactly the same", async ({ page }) => {
    const backend = await open(page, { orgs: { acme: "okta" } });
    await page.goto("/login");
    const notice = page.getByRole("status");

    await page.getByLabel("Organization").fill("no-such-org");
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await expect(notice).toContainText("Single sign-on isn't available for this organization.");
    const first = await notice.innerText();

    await page.getByLabel("Organization").fill("exists-but-no-sso");
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await expect(notice).toHaveText(first);
    await expect(page.getByRole("button", { name: /Continue with/ })).toHaveCount(0);
    expect(backend.callsTo("/auth/no-such-org/sso-options")).toHaveLength(1);
  });

  test("rejects a malformed organization name without calling the server", async ({ page }) => {
    const backend = await open(page);
    await page.goto("/login");
    await page.getByLabel("Organization").fill("Bad Name!");
    await page.getByRole("button", { name: "Continue", exact: true }).click();

    await expect(page.getByRole("alert")).toContainText("short name");
    expect(backend.calls.filter((c) => c.path.includes("sso-options"))).toHaveLength(0);
  });
});

// ======================================================================= the round trip

test.describe("SSO sign-in round trip", () => {
  test("signs in, completes the callback exactly once under StrictMode, and keeps secrets out of URLs", async ({ page }) => {
    const backend = await open(page);
    await page.goto("/login");
    await page.getByLabel("Organization").fill("acme");
    await page.getByRole("button", { name: "Continue", exact: true }).click();

    // a double click must start only ONE login
    await page.getByRole("button", { name: "Continue with Okta" }).dblclick();

    await expect(page).toHaveURL(/\/ask$/, { timeout: 15_000 });
    expect(backend.callsTo("/auth/acme/login")).toHaveLength(1);
    const begin = backend.callsTo("/auth/acme/login")[0];
    expect(new URLSearchParams(begin.query).get("redirect_uri")).toBe(`${new URL(page.url()).origin}/o/acme/callback`);

    // the exchange ran exactly once even though StrictMode mounts the page twice
    const callbacks = backend.callsTo("/auth/callback");
    expect(callbacks).toHaveLength(1);
    expect(callbacks[0].body).toEqual({ org_slug: "acme", code: "auth-code-123", state: "state-abc", transaction_id: "txn-opaque-handle" });
    expect(callbacks[0].body).not.toHaveProperty("code_verifier");

    // the session is applied like any other, and /auth/me was loaded
    expect(backend.callsTo("/auth/me").length).toBeGreaterThanOrEqual(1);
    expect(await page.evaluate(() => localStorage.getItem("ekip.refresh_token"))).toBe("rt-sso");
    // no tokens in any URL the browser requested or shows; the code is gone from the address bar
    expect(page.url()).not.toMatch(/code=|state=|token/);
    for (const url of backend.urls) expect(url).not.toMatch(/access_token|refresh_token|rt-sso/);
    // the pending-login reference is cleared, and never held a secret
    expect(await page.evaluate(() => sessionStorage.getItem("ekip.sso.transaction"))).toBeNull();
  });

  test("stores only a short-lived reference to the server-side attempt before leaving", async ({ page }) => {
    await open(page);
    // Record every browser-storage write, surviving the trip to the provider and back.
    const writes: Array<{ area: string; key: string; value: string }> = [];
    await page.exposeFunction("recordStorageWrite", (area: string, key: string, value: string) => {
      writes.push({ area, key, value });
    });
    await page.addInitScript(() => {
      for (const [area, storage] of [["session", window.sessionStorage], ["local", window.localStorage]] as const) {
        const original = storage.setItem.bind(storage);
        storage.setItem = (key: string, value: string) => {
          void (window as unknown as { recordStorageWrite: (a: string, k: string, v: string) => void }).recordStorageWrite(area, key, value);
          original(key, value);
        };
      }
    });
    await page.goto("/o/acme/login");
    await page.getByRole("button", { name: "Continue with Okta" }).click();
    await expect(page).toHaveURL(/\/ask$/, { timeout: 15_000 });

    const pending = writes.filter((w) => w.area === "session" && w.key === "ekip.sso.transaction");
    expect(pending).toHaveLength(1);
    const stored = JSON.parse(pending[0].value);
    expect(Object.keys(stored).sort()).toEqual(["slug", "startedAt", "state", "transactionId"]);
    expect(stored).toMatchObject({ slug: "acme", state: "state-abc", transactionId: "txn-opaque-handle" });
    // nothing else was written to sessionStorage, and no secret-looking value anywhere in storage
    expect(writes.filter((w) => w.area === "session")).toHaveLength(1);
    expect(JSON.stringify(writes)).not.toMatch(/verifier|nonce|auth-code-123/);
    // the one thing persisted afterwards is the refresh token, as for a password login
    expect(writes.filter((w) => w.area === "local").map((w) => w.key)).toEqual(["ekip.refresh_token"]);
  });

  test("a cancelled sign-in at the provider says so and never calls the server", async ({ page }) => {
    const backend = await open(page, { idp: "cancel" });
    await page.goto("/o/acme/login");
    await page.getByRole("button", { name: "Continue with Okta" }).click();

    await expect(page.getByTestId("sso-callback-error")).toContainText("Sign-in cancelled");
    expect(backend.callsTo("/auth/callback")).toHaveLength(0);
    expect(page.url()).not.toMatch(/error=|code=|state=/);
    expect(await page.evaluate(() => sessionStorage.getItem("ekip.sso.transaction"))).toBeNull();
    await page.getByRole("link", { name: "Try again" }).click();
    await expect(page).toHaveURL(/\/o\/acme\/login$/);
    await expect(page.getByRole("button", { name: "Continue with Okta" })).toBeVisible();
  });

  test("another provider error is reported plainly", async ({ page }) => {
    const backend = await open(page, { idp: "server_error" });
    await page.goto("/o/acme/login");
    await page.getByRole("button", { name: "Continue with Okta" }).click();

    await expect(page.getByTestId("sso-callback-error")).toContainText("identity provider reported a problem");
    expect(backend.callsTo("/auth/callback")).toHaveLength(0);
  });

  test("a response whose state differs from the stored attempt is refused before any exchange", async ({ page }) => {
    const backend = await open(page, { idp: "state_mismatch" });
    await page.goto("/o/acme/login");
    await page.getByRole("button", { name: "Continue with Okta" }).click();

    await expect(page.getByTestId("sso-callback-error")).toContainText("didn't match");
    expect(backend.callsTo("/auth/callback")).toHaveLength(0);
    expect(await page.evaluate(() => localStorage.getItem("ekip.refresh_token"))).toBeNull();
  });

  test("opening the callback address with no attempt in this tab explains and does nothing", async ({ page }) => {
    const backend = await open(page);
    await page.goto("/o/acme/callback?code=stolen-code&state=whatever");

    await expect(page.getByTestId("sso-callback-error")).toContainText("can't be continued");
    expect(backend.callsTo("/auth/callback")).toHaveLength(0);
    expect(page.url()).not.toMatch(/code=|state=/);
  });

  test("opening the callback address with nothing at all is harmless", async ({ page }) => {
    const backend = await open(page);
    await page.goto("/o/acme/callback");

    await expect(page.getByTestId("sso-callback-error")).toContainText("Nothing to complete");
    expect(backend.callsTo("/auth/callback")).toHaveLength(0);
  });

  for (const [code, text] of [
    ["auth.not_provisioned", "haven't been given access"],
    ["auth.sso_transaction_invalid", "expired or was already used"],
    ["auth.idp_unreachable", "couldn't be reached"],
    ["user.inactive", "inactive"],
    ["sso.unavailable", "isn't available"],
  ] as const) {
    test(`shows a clear message when the server answers ${code}`, async ({ page }) => {
      await open(page, { callbackError: { status: 403, error_code: code, message: "server words" } });
      await page.goto("/o/acme/login");
      await page.getByRole("button", { name: "Continue with Okta" }).click();

      await expect(page.getByTestId("sso-callback-error")).toContainText(text);
      expect(await page.evaluate(() => localStorage.getItem("ekip.refresh_token"))).toBeNull();
      expect(await page.evaluate(() => sessionStorage.getItem("ekip.sso.transaction"))).toBeNull();
      await expect(page).toHaveURL(/\/o\/acme\/callback$/);
    });
  }

  test("a failure to start shows an error and lets the person try again", async ({ page }) => {
    await open(page, { beginError: { status: 503, error_code: "auth.idp_unreachable", message: "down" } });
    await page.goto("/o/acme/login");
    const button = page.getByRole("button", { name: "Continue with Okta" });
    await button.click();

    await expect(page.getByRole("alert")).toContainText("couldn't be reached");
    await expect(button).toBeEnabled();
    expect(await page.evaluate(() => sessionStorage.getItem("ekip.sso.transaction"))).toBeNull();
  });

  test("the direct organization page offers the provider, or a neutral message and a password link", async ({ page }) => {
    await open(page, { orgs: { acme: "entra_id" } });
    await page.goto("/o/acme/login");
    await expect(page.getByRole("button", { name: "Continue with Microsoft" })).toBeVisible();
    await expect(page.getByLabel("Organization")).toHaveCount(0);

    await page.goto("/o/ghost/login");
    await expect(page.getByRole("status")).toContainText("Single sign-on isn't available");
    await page.getByRole("link", { name: "Sign in with a password" }).click();
    await expect(page).toHaveURL(/\/login$/);

    await page.goto("/o/Bad_Slug!/login");
    await expect(page.getByRole("alert")).toContainText("isn't valid");
  });
});

// ======================================================================= settings

test.describe("SSO settings", () => {
  test("shows the real provider and enabled state, the shareable login URL and the redirect URI", async ({ page }) => {
    await open(page, { ssoConfig: { provider: "okta", enabled: true } }, true);
    await page.goto("/settings/sso");

    await expect(page.getByTestId("sso-status")).toHaveText("Enabled");
    await expect(page.getByTestId("sso-provider")).toHaveText("Okta");
    const origin = new URL(page.url()).origin;
    await expect(page.getByLabel("Organization SSO login URL", { exact: true })).toHaveValue(`${origin}/o/acme/login`);
    await expect(page.getByLabel("Redirect URI to register at your identity provider", { exact: true })).toHaveValue(`${origin}/o/acme/callback`);
  });

  test("disabling asks for confirmation, sends enabled=false and can be reversed", async ({ page }) => {
    const backend = await open(page, { ssoConfig: { provider: "okta", enabled: true } }, true);
    await page.goto("/settings/sso");

    await page.getByRole("button", { name: "Disable SSO" }).click();
    await expect(page.getByRole("dialog")).toContainText("Password sign-in is not affected");
    await page.getByRole("dialog").getByRole("button", { name: "Disable SSO" }).click();
    await expect(page.getByTestId("sso-status")).toHaveText("Disabled");
    expect(backend.calls.find((c) => c.method === "PATCH")!.body).toEqual({ enabled: false });

    await page.getByRole("button", { name: "Enable SSO" }).click();
    await expect(page.getByTestId("sso-status")).toHaveText("Enabled");
    expect(backend.calls.filter((c) => c.method === "PATCH").at(-1)!.body).toEqual({ enabled: true });
  });

  test("changing the issuer needs an explicit confirmation of the unlinking", async ({ page }) => {
    const backend = await open(page, { ssoConfig: { provider: "okta", enabled: true }, patchConflict: { identity_link_count: 3 } }, true);
    await page.goto("/settings/sso");
    await page.getByRole("button", { name: "Edit configuration" }).click();
    await page.getByLabel("Issuer URL").fill("https://other.okta.com");
    await page.getByRole("button", { name: "Save changes" }).click();

    await expect(page.getByRole("dialog")).toContainText("unlinks 3 people");
    expect(backend.calls.filter((c) => c.method === "PATCH")).toHaveLength(1);
    await page.getByRole("dialog").getByRole("button", { name: "Unlink and save" }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    const patches = backend.calls.filter((c) => c.method === "PATCH");
    expect(patches).toHaveLength(2);
    expect(patches[1].body).toEqual({ issuer_url: "https://other.okta.com", confirm_identity_reset: true });
  });

  test("editing without touching the secret never sends one", async ({ page }) => {
    const backend = await open(page, { ssoConfig: { provider: "okta", enabled: true } }, true);
    await page.goto("/settings/sso");
    await page.getByRole("button", { name: "Edit configuration" }).click();
    await page.getByLabel("Client ID").fill("client-2");
    await page.getByRole("button", { name: "Save changes" }).click();

    await expect(page.getByRole("button", { name: "Edit configuration" })).toBeVisible();
    expect(backend.calls.find((c) => c.method === "PATCH")!.body).toEqual({ client_id: "client-2" });
  });

  test("first-time setup creates an enabled OIDC configuration and offers no SAML", async ({ page }) => {
    const backend = await open(page, { ssoConfig: null }, true);
    await page.goto("/settings/sso");

    await expect(page.getByLabel("Provider", { exact: true }).locator("option")).toHaveCount(4);
    await expect(page.getByText("SAML")).toHaveCount(0);
    await page.getByLabel("Provider", { exact: true }).selectOption("google_workspace");
    await page.getByLabel("Issuer URL").fill("https://accounts.google.com");
    await page.getByLabel("Client ID").fill("abc.apps.googleusercontent.com");
    await page.getByLabel("Client secret").fill("s3cret");
    await page.getByRole("button", { name: "Save configuration" }).click();

    await expect(page.getByTestId("sso-status")).toHaveText("Enabled");
    expect(backend.calls.find((c) => c.method === "POST" && c.path.endsWith("/sso/configure"))!.body).toMatchObject({
      provider: "google_workspace", protocol: "oidc", enabled: true, client_secret_ref: "s3cret",
    });
  });

  test("a member without tenancy:manage can look but not change", async ({ page }) => {
    await open(page, { ssoConfig: { provider: "okta", enabled: true }, permissions: ["incident:read"] }, true);
    await page.goto("/settings/sso");

    await expect(page.getByTestId("sso-provider")).toHaveText("Okta");
    await expect(page.getByRole("button", { name: "Edit configuration" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Disable SSO" })).toHaveCount(0);
  });
});
