import { test, expect } from "@playwright/test";
import { loginViaUI, signupViaUI } from "./fixtures/actions";
import { ORG_ALPHA } from "./fixtures/testUsers";

test.describe("Authentication", () => {
  test("an unauthenticated visitor hitting a protected route is redirected to login", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByRole("heading", { name: "Sign in to EKIP" })).toBeVisible();
  });

  test("signup creates a real account (new org + admin user) and reaches the app", async ({ page }) => {
    const unique = Date.now();
    await signupViaUI(page, {
      name: "E2E Fresh Signup",
      email: `e2e-fresh-signup-${unique}@example.com`,
      password: "FreshSignup123!",
      orgName: `E2E Fresh Org ${unique}`,
      orgSlug: `e2e-fresh-org-${unique}`,
    });
    await expect(page).toHaveURL(/\/ask$/);
    // A brand-new signup gets the full admin role (core.auth.service.signup)
    // -- Connectors is gated by tenancy:manage, so its nav link being
    // visible proves the real permission set came back from `GET /auth/me`,
    // not a stub.
    await expect(page.getByRole("link", { name: "Connectors" })).toBeVisible();
  });

  test("login with valid credentials reaches the authenticated app", async ({ page }) => {
    await loginViaUI(page, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);
    await expect(page).toHaveURL(/\/ask$/);
    await expect(page.getByRole("button", { name: `Account menu for ${ORG_ALPHA.admin.name}` })).toBeVisible();
  });

  test("login with a wrong password fails without authenticating", async ({ page }) => {
    await page.goto("/login");
    await page.getByLabel("Work email").fill(ORG_ALPHA.admin.email);
    await page.getByLabel("Password").fill("definitely-wrong-password");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByText("Sign in failed")).toBeVisible();
    await expect(page).toHaveURL(/\/login$/);
  });

  test("a 401 from an authenticated API call clears the session and returns to login", async ({ page }) => {
    // core.auth.service issues short-lived (jwt_expiry_minutes) access
    // tokens, so waiting for a real one to expire is impractical for a
    // test. This intercepts one real, already-authenticated request and
    // substitutes a 401 EKIPError body -- the exact shape `app.api.errors.
    // ekip_error_handler` produces -- to deterministically exercise
    // `api/client.ts`'s real `clearSessionAndNotifyExpired` path (the
    // Phase 1 fix) rather than waiting out a token lifetime. Everything
    // downstream (AuthContext clearing `user`, ProtectedRoute redirecting)
    // is the genuine app reacting to that response, not simulated.
    await loginViaUI(page, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);

    await page.route("**/observability/agents", (route) =>
      route.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({ error_code: "auth.invalid_token", message: "Invalid or expired access token." }),
      }),
    );
    await page.goto("/agents");

    await expect(page).toHaveURL(/\/login$/, { timeout: 10_000 });
  });
});
