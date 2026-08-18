import { expect, type APIRequestContext, type Page } from "@playwright/test";

/** The real FastAPI backend this whole suite runs against (see
 * `frontend/e2e/README.md`) -- distinct from `baseURL` (the Vite dev
 * server) because the two are deliberately different origins/ports here. */
export const API_BASE_URL = process.env.E2E_API_BASE_URL ?? "http://127.0.0.1:8010";

/** Fills and submits the real login form, waiting for the app to actually
 * navigate away from /login (a genuine authenticated session, not a stub). */
export async function loginViaUI(page: Page, email: string, password: string): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Work email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).not.toHaveURL(/\/login$/, { timeout: 30_000 });
}

/** Logs in through the real `POST /auth/login` directly (no browser), for
 * tests that need a genuine access token to attempt a request outside the
 * UI -- e.g. proving the backend itself denies a permission-gated call,
 * not just that the frontend hides the button. Never used in place of a UI
 * login for the app-behavior assertions themselves. */
export async function apiLogin(request: APIRequestContext, email: string, password: string): Promise<string> {
  const response = await request.post(`${API_BASE_URL}/auth/login`, { data: { email, password } });
  expect(response.ok(), `login failed: ${response.status()} ${await response.text()}`).toBeTruthy();
  const body = await response.json();
  return body.access_token as string;
}

/** Reads the `organization_id` claim straight out of a real access token
 * (`core.auth.service._issue_access_token`'s own claims) -- no separate
 * "whoami" round trip needed. */
export function decodeOrganizationId(accessToken: string): string {
  const payload = accessToken.split(".")[1];
  const json = Buffer.from(payload, "base64url").toString("utf-8");
  return JSON.parse(json).organization_id as string;
}

/** Fills and submits the real signup form (always creates a brand-new
 * organization -- `core.auth.service.signup`'s own documented scope). */
export async function signupViaUI(
  page: Page,
  data: { name: string; email: string; password: string; orgName: string; orgSlug: string },
): Promise<void> {
  await page.goto("/signup");
  await page.getByLabel("Your name").fill(data.name);
  await page.getByLabel("Work email").fill(data.email);
  await page.getByLabel("Password").fill(data.password);
  await page.getByLabel("Organization name").fill(data.orgName);
  await page.getByLabel("Organization URL slug").fill(data.orgSlug);
  await page.getByRole("button", { name: "Create account" }).click();
  // `signup` is a genuinely heavy operation (new org + project + user +
  // password hash + role/permission bootstrap + session issuance, ~15-20
  // sequential queries) -- observed at ~9-10s end-to-end against this
  // environment's real remote dev database, so this needs real headroom,
  // not the fast ~1-2s a plain login takes.
  await expect(page).not.toHaveURL(/\/signup$/, { timeout: 30_000 });
}

export async function logoutViaUI(page: Page): Promise<void> {
  await page.getByRole("button", { name: /Account menu/ }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/, { timeout: 10_000 });
}
