import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * Browser tests for password reset and change: the "Forgot password?" link, the forgot / reset
 * pages, Settings -> Security, and the 12-128 character policy on signup and invitation acceptance.
 *
 * Like `sso-login-ui.spec.ts` this answers every backend call with a small stateful fake
 * (`page.route`), so it needs only the Vite DEV server (React StrictMode on) built with
 * `VITE_USE_MOCK_DATA=false VITE_API_BASE_URL=http://127.0.0.1:8010`. The security rules themselves
 * (single-use tokens, session revocation, policy, rate limits) are enforced and tested server-side
 * (`tests/core/auth/test_password_*`, `tests/api/test_password_*`); no email is involved here.
 */

const API = process.env.E2E_API_BASE_URL ?? "http://127.0.0.1:8010";
const ORG = "11111111-1111-4111-8111-111111111111";
const ME = "22222222-2222-4222-8222-222222222222";

interface ApiErr {
  status: number;
  error_code: string;
  message: string;
}

interface Options {
  forgotError?: ApiErr;
  resetError?: ApiErr;
  changeError?: ApiErr;
  signupError?: ApiErr;
  loginError?: ApiErr;
  forgotDelayMs?: number;
  permissions?: string[];
}

function makeBackend(options: Options = {}) {
  const calls: Array<{ method: string; path: string; body: unknown; query: string }> = [];
  const json = (route: Route, status: number, body: unknown) =>
    route.fulfill({ status, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(body) });
  const claims = () => Buffer.from(JSON.stringify({ sub: ME, organization_id: ORG, exp: 4102444800 })).toString("base64url");

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
          "access-control-allow-methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
        },
      });
    }
    const body = req.postData() ? JSON.parse(req.postData()!) : undefined;
    calls.push({ method, path, body, query: url.search });

    if (path === "/auth/password/forgot") {
      if (options.forgotDelayMs) await new Promise((r) => setTimeout(r, options.forgotDelayMs));
      if (options.forgotError) return json(route, options.forgotError.status, options.forgotError);
      return json(route, 202, { status: "reset_requested", message: "generic" });
    }
    if (path === "/auth/password/reset") {
      if (options.resetError) return json(route, options.resetError.status, options.resetError);
      return json(route, 200, { status: "password_reset", message: "done" });
    }
    if (path === "/auth/password/change") {
      if (options.changeError) return json(route, options.changeError.status, options.changeError);
      return json(route, 200, { status: "complete", access_token: `h.${claims()}.s`, refresh_token: "rt-after-change", token_type: "bearer", expires_in: 900 });
    }
    if (path === "/auth/signup") {
      if (options.signupError) return json(route, options.signupError.status, options.signupError);
      return json(route, 202, { status: "verification_sent", message: "ok" });
    }
    if (path === "/auth/login") {
      if (options.loginError) return json(route, options.loginError.status, options.loginError);
      return json(route, 200, { status: "complete", access_token: `h.${claims()}.s`, refresh_token: "rt-pw", token_type: "bearer", expires_in: 900 });
    }
    if (path === "/auth/refresh") {
      return json(route, 200, { access_token: `h.${claims()}.s`, refresh_token: "rt2", token_type: "bearer", expires_in: 3600 });
    }
    if (path === "/auth/me") {
      return json(route, 200, {
        id: ME, email: "me@example.com", display_name: "Me", is_active: true, roles: ["viewer"],
        permissions: options.permissions ?? ["incident:read", "observability:read"],
      });
    }
    if (path === "/auth/organizations") return json(route, 200, { organizations: [{ id: ORG, name: "Acme", slug: "acme" }] });
    if (path === "/organizations") return json(route, 200, [{ id: ORG, name: "Acme", slug: "acme" }]);
    if (path.match(/^\/invitations\/[^/]+\/accept$/)) {
      if (options.signupError) return json(route, options.signupError.status, options.signupError);
      return json(route, 202, { status: "verification_sent", message: "ok" });
    }
    return json(route, 200, []);
  }

  return { handle, calls, callsTo: (p: string) => calls.filter((c) => c.path === p) };
}

async function open(page: Page, options: Options = {}, signedIn = false) {
  const backend = makeBackend(options);
  if (signedIn) await page.addInitScript(() => localStorage.setItem("ekip.refresh_token", "rt1"));
  await page.route(`${API}/**`, backend.handle);
  return backend;
}

const GOOD = "a long enough password here";

// ======================================================================= login + forgot

test.describe("Forgot password", () => {
  test("the sign-in page links to it and password sign-in still works", async ({ page }) => {
    const backend = await open(page);
    await page.goto("/login");
    await expect(page.getByRole("link", { name: "Forgot password?" })).toBeVisible();

    await page.getByLabel("Work email").fill("me@example.com");
    await page.getByLabel("Password", { exact: true }).fill("any existing password");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page).toHaveURL(/\/ask$/);
    expect(backend.callsTo("/auth/login")).toHaveLength(1);

    await page.evaluate(() => localStorage.clear());
  });

  test("shows the same generic confirmation for any address and sends exactly one request", async ({ page }) => {
    const backend = await open(page, { forgotDelayMs: 300 });
    await page.goto("/login");
    await page.getByRole("link", { name: "Forgot password?" }).click();
    await expect(page).toHaveURL(/\/forgot-password$/);

    await page.getByLabel("Email").fill("whoever@example.com");
    await page.getByRole("button", { name: "Send reset link" }).dblclick(); // a double click is one request
    const sent = page.getByTestId("forgot-password-sent");
    await expect(sent).toBeVisible();
    await expect(sent).toContainText("If whoever@example.com belongs to an account that signs in with a password");
    await expect(sent).toContainText("single sign-on");
    expect(backend.callsTo("/auth/password/forgot")).toHaveLength(1);
    expect(backend.callsTo("/auth/password/forgot")[0].body).toEqual({ email: "whoever@example.com" });
    await expect(sent).not.toContainText("we sent"); // never claims an email was sent
  });

  test("an unreachable email service or a rate limit is reported plainly", async ({ page }) => {
    await open(page, { forgotError: { status: 503, error_code: "email.not_configured", message: "x" } });
    await page.goto("/forgot-password");
    await page.getByLabel("Email").fill("a@example.com");
    await page.getByRole("button", { name: "Send reset link" }).click();
    await expect(page.getByRole("alert")).toContainText("Email isn't set up");

    await page.unroute(`${API}/**`);
    await open(page, { forgotError: { status: 429, error_code: "rate_limited.ip", message: "x" } });
    await page.getByRole("button", { name: "Send reset link" }).click();
    await expect(page.getByRole("alert")).toContainText("Too many attempts");
  });
});

// ======================================================================= reset

test.describe("Reset password", () => {
  test("takes the token from the URL fragment, removes it, and submits it once under StrictMode", async ({ page }) => {
    const backend = await open(page);
    await page.goto("/reset-password#token=tok-abc_123");

    await expect(page.getByLabel("New password", { exact: true })).toBeVisible();
    expect(page.url()).not.toContain("token"); // gone from the address bar
    await page.getByLabel("New password", { exact: true }).fill(GOOD);
    await page.getByLabel("Confirm new password").fill(GOOD);
    await page.getByRole("button", { name: "Change password" }).dblclick();

    await expect(page.getByTestId("reset-success")).toContainText("signed out of every device");
    const calls = backend.callsTo("/auth/password/reset");
    expect(calls).toHaveLength(1);
    expect(calls[0].body).toEqual({ token: "tok-abc_123", new_password: GOOD });
    expect(calls[0].query).toBe(""); // never in a query string
    // no session was created, nothing stored
    expect(await page.evaluate(() => localStorage.getItem("ekip.refresh_token"))).toBeNull();
    expect(await page.evaluate(() => JSON.stringify({ ...sessionStorage }))).not.toContain("tok-abc_123");
    await page.getByRole("link", { name: "Go to sign in" }).click();
    await expect(page).toHaveURL(/\/login$/);
  });

  test("validates length and confirmation in the browser before calling the server", async ({ page }) => {
    const backend = await open(page);
    await page.goto("/reset-password#token=t");
    const submit = page.getByRole("button", { name: "Change password" });

    // the field's own minLength blocks submit, so fill via evaluate-free path: type 11 chars and try
    await page.getByLabel("New password", { exact: true }).fill("a".repeat(11));
    await page.getByLabel("Confirm new password").fill("a".repeat(11));
    await submit.click();
    expect(backend.callsTo("/auth/password/reset")).toHaveLength(0);

    await page.getByLabel("New password", { exact: true }).fill(GOOD);
    await page.getByLabel("Confirm new password").fill(GOOD + "x");
    await submit.click();
    await expect(page.getByRole("alert")).toContainText("don't match");
    expect(backend.callsTo("/auth/password/reset")).toHaveLength(0);
  });

  test("a policy error from the server keeps the form so the same link can be reused", async ({ page }) => {
    await open(page, { resetError: { status: 400, error_code: "auth.password_same_as_email", message: "x" } });
    await page.goto("/reset-password#token=t");
    await page.getByLabel("New password", { exact: true }).fill(GOOD);
    await page.getByLabel("Confirm new password").fill(GOOD);
    await page.getByRole("button", { name: "Change password" }).click();

    await expect(page.getByRole("alert")).toContainText("can't be the same as your email");
    await expect(page.getByLabel("New password", { exact: true })).toBeVisible(); // still on the form
  });

  for (const [code, reason, text] of [
    ["auth.reset_token_expired", "expired", "This link has expired"],
    ["auth.reset_token_used", "used", "already used"],
    ["auth.reset_token_invalid", "invalid", "isn't valid"],
  ] as const) {
    test(`shows a dead-link page for ${code} with a way to request a new one`, async ({ page }) => {
      await open(page, { resetError: { status: code === "auth.reset_token_used" ? 409 : 400, error_code: code, message: "x" } });
      await page.goto("/reset-password#token=t");
      await page.getByLabel("New password", { exact: true }).fill(GOOD);
      await page.getByLabel("Confirm new password").fill(GOOD);
      await page.getByRole("button", { name: "Change password" }).click();

      const dead = page.getByTestId("reset-dead");
      await expect(dead).toHaveAttribute("data-reason", reason);
      await expect(dead).toContainText(text);
      await page.getByRole("link", { name: "Request a new link" }).click();
      await expect(page).toHaveURL(/\/forgot-password$/);
    });
  }

  test("a link without a token is reported immediately and nothing is sent", async ({ page }) => {
    const backend = await open(page);
    await page.goto("/reset-password");
    await expect(page.getByTestId("reset-dead")).toHaveAttribute("data-reason", "missing");
    expect(backend.callsTo("/auth/password/reset")).toHaveLength(0);
  });
});

// ======================================================================= change password

test.describe("Settings -> Security", () => {
  test("is available to every signed-in user and swaps in the new session after a change", async ({ page }) => {
    const backend = await open(page, { permissions: ["incident:read"] }, true); // no admin rights
    await page.goto("/settings/security");
    await expect(page.getByRole("link", { name: "Security" })).toBeVisible();

    await page.getByLabel("Current password").fill("my old password");
    await page.getByLabel("New password", { exact: true }).fill(GOOD);
    await page.getByLabel("Confirm new password").fill(GOOD);
    await page.getByRole("button", { name: "Change password" }).click();

    await expect(page.getByText("You were signed out of your other devices")).toBeVisible();
    const calls = backend.callsTo("/auth/password/change");
    expect(calls).toHaveLength(1);
    expect(calls[0].body).toEqual({ current_password: "my old password", new_password: GOOD });
    // the response's NEW refresh token replaced the stored one; the form was cleared
    expect(await page.evaluate(() => localStorage.getItem("ekip.refresh_token"))).toBe("rt-after-change");
    await expect(page.getByLabel("Current password")).toHaveValue("");
    await expect(page).toHaveURL(/\/settings\/security$/); // still signed in
  });

  test("refuses mismatches and an unchanged password before calling the server", async ({ page }) => {
    const backend = await open(page, {}, true);
    await page.goto("/settings/security");
    await page.getByLabel("Current password").fill(GOOD);
    await page.getByLabel("New password", { exact: true }).fill(GOOD);
    await page.getByLabel("Confirm new password").fill(GOOD);
    await page.getByRole("button", { name: "Change password" }).click();
    await expect(page.getByRole("alert")).toContainText("different from your current");

    await page.getByLabel("Current password").fill("my old password");
    await page.getByLabel("Confirm new password").fill(GOOD + "!");
    await page.getByRole("button", { name: "Change password" }).click();
    await expect(page.getByRole("alert")).toContainText("don't match");
    expect(backend.callsTo("/auth/password/change")).toHaveLength(0);
  });

  for (const [code, text] of [
    ["auth.current_password_incorrect", "Your current password is incorrect"],
    ["auth.no_password", "single sign-on"],
    ["rate_limited.user", "Too many attempts"],
  ] as const) {
    test(`maps ${code} to a clear message and keeps the session`, async ({ page }) => {
      await open(page, { changeError: { status: code === "auth.no_password" ? 400 : code.startsWith("rate") ? 429 : 403, error_code: code, message: "x" } }, true);
      await page.goto("/settings/security");
      await page.getByLabel("Current password").fill("my old password");
      await page.getByLabel("New password", { exact: true }).fill(GOOD);
      await page.getByLabel("Confirm new password").fill(GOOD);
      await page.getByRole("button", { name: "Change password" }).click();

      await expect(page.getByRole("alert")).toContainText(text);
      expect(await page.evaluate(() => localStorage.getItem("ekip.refresh_token"))).not.toBeNull(); // not signed out
    });
  }
});

// ======================================================================= policy on signup / invitations / login

test.describe("Password policy in the UI", () => {
  test("signup refuses fewer than 12 characters and accepts 12", async ({ page }) => {
    const backend = await open(page);
    await page.goto("/signup");
    await page.getByLabel("Your name").fill("Pat");
    await page.getByLabel("Work email").fill("pat@corp.test");
    await page.getByLabel("Organization name").fill("Corp");
    await page.getByLabel("Password").fill("a".repeat(11));
    await page.getByRole("button", { name: "Create account" }).click();
    expect(backend.callsTo("/auth/signup")).toHaveLength(0);

    await page.getByLabel("Password").fill("a".repeat(12));
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page.getByText("Check your email")).toBeVisible();
    expect(backend.callsTo("/auth/signup")).toHaveLength(1);
  });

  test("a server-side policy error on signup is shown in plain words", async ({ page }) => {
    await open(page, { signupError: { status: 400, error_code: "auth.password_too_long", message: "x" } });
    await page.goto("/signup");
    await page.getByLabel("Your name").fill("Pat");
    await page.getByLabel("Work email").fill("pat@corp.test");
    await page.getByLabel("Organization name").fill("Corp");
    await page.getByLabel("Password").fill("a".repeat(20));
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page.getByText("Use at most 128 characters.")).toBeVisible();
  });

  test("invitation acceptance uses the same 12-character rule", async ({ page }) => {
    const backend = await open(page);
    await page.goto("/invitations/inv-1/accept#token=tok");
    await page.getByLabel("Choose a password").fill("a".repeat(11));
    await page.getByRole("button", { name: "Accept invitation" }).click();
    expect(backend.calls.filter((c) => c.path.endsWith("/accept"))).toHaveLength(0);
    await page.getByLabel("Choose a password").fill("a".repeat(12));
    await page.getByRole("button", { name: "Accept invitation" }).click();
    await expect(page.getByText("Check your email")).toBeVisible();
  });

  test("sign-in is never length-checked in the browser (an existing short password must still work)", async ({ page }) => {
    const backend = await open(page);
    await page.goto("/login");
    await page.getByLabel("Work email").fill("me@example.com");
    await page.getByLabel("Password", { exact: true }).fill("short8chr");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page).toHaveURL(/\/ask$/);
    expect(backend.callsTo("/auth/login")[0].body).toEqual({ email: "me@example.com", password: "short8chr" });
  });

  test("a suspended organization is explained at sign-in", async ({ page }) => {
    await open(page, { loginError: { status: 403, error_code: "auth.organization_suspended", message: "x" } });
    await page.goto("/login");
    await page.getByLabel("Work email").fill("me@example.com");
    await page.getByLabel("Password", { exact: true }).fill("whatever it is here");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    // Phase 6: a refusal because the organization is suspended lands on the dedicated explanation page
    // (it used to be a toast on the sign-in form).
    await expect(page).toHaveURL(/\/suspended$/);
    await expect(page.getByRole("heading", { name: "Your organization is suspended" })).toBeVisible();
    await expect(page.getByText("Your data has not been deleted.")).toBeVisible();
  });
});

// ======================================================================= a session ended elsewhere

test.describe("Session ended by a password reset elsewhere", () => {
  test("an open page returns to sign-in and clears its stored session when the server says it was revoked", async ({ page }) => {
    // Signed in, then every API call starts answering 403 auth.session_revoked (what the server does
    // after a reset/change ends this session from another device).
    let revoked = false;
    const backend = makeBackend();
    await page.addInitScript(() => localStorage.setItem("ekip.refresh_token", "rt1"));
    await page.route(`${API}/**`, async (route) => {
      if (revoked && route.request().method() !== "OPTIONS" && new URL(route.request().url()).pathname === "/auth/organizations") {
        return route.fulfill({
          status: 403,
          contentType: "application/json",
          headers: { "access-control-allow-origin": "*" },
          body: JSON.stringify({ error_code: "auth.session_revoked", message: "ended", detail: null }),
        });
      }
      return backend.handle(route);
    });
    await page.goto("/settings/organization");
    await expect(page).toHaveURL(/\/settings\/organization$/);

    revoked = true;
    await page.reload(); // any request made with the dead session now gets the 403
    await expect(page).toHaveURL(/\/login$/, { timeout: 15_000 });
    expect(await page.evaluate(() => localStorage.getItem("ekip.refresh_token"))).toBeNull();
  });
});
