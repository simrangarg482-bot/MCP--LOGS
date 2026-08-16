import { expect, type Page } from "@playwright/test";

/** Fills and submits the real login form, waiting for the app to actually
 * navigate away from /login (a genuine authenticated session, not a stub). */
export async function loginViaUI(page: Page, email: string, password: string): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Work email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).not.toHaveURL(/\/login$/, { timeout: 10_000 });
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
