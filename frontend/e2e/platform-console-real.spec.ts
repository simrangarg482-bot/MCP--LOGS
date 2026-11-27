import { expect, test } from "@playwright/test";

/**
 * REAL-backend end-to-end of the whole lifecycle: an operator signs in to the console, suspends a real
 * organization, its real administrator is shut out and told why, the operator reactivates it, the
 * administrator's old session stays dead and a fresh sign-in works.
 *
 * Needs (all LOCAL, throw-away): the API on E2E_API_BASE_URL started with PLATFORM_ADMIN_ENABLED=true and
 * CORS for the dev server, the Vite dev server (`VITE_USE_MOCK_DATA=false`), and these variables naming an
 * organization, its administrator and an operator created for the run (scripts/platform_operator.py).
 * Skipped otherwise. Nothing is mocked: this is the check that the console's idea of the API is the API.
 */
const ORG_ID = process.env.E2E_SMOKE_ORG_ID ?? "";
const ADMIN = process.env.E2E_SMOKE_ADMIN ?? "";
const OPS = process.env.E2E_SMOKE_OPS ?? "";
const PASSWORD = process.env.E2E_SMOKE_PASSWORD ?? "";
const TAG = process.env.E2E_SMOKE_TAG ?? "";

test.setTimeout(240_000); // every call is slow without Redis (the rate limiter waits to fail open)
test.skip(!ORG_ID || !ADMIN || !OPS || !PASSWORD, "needs E2E_SMOKE_* (a local organization, administrator and operator)");

test("operator suspends and reactivates a real organization; the tenant admin is shut out and back in", async ({ browser }) => {
  const opsContext = await browser.newContext();
  const adminContext = await browser.newContext();
  const ops = await opsContext.newPage();
  const admin = await adminContext.newPage();

  // --- the tenant administrator is working normally
  await admin.goto("/login");
  await admin.getByLabel("Work email").fill(ADMIN);
  await admin.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await admin.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(admin).toHaveURL(/\/ask$/, { timeout: 30_000 });
  await admin.goto("/settings/organization");
  await expect(admin.getByTestId("organization-status")).toHaveText("Onboarding");
  await expect(admin.getByText("managed by the EKIP platform team")).toBeVisible();

  // --- the operator signs in to the console and finds the organization
  await ops.goto("/platform/login");
  await ops.getByLabel("Operator email").fill(OPS);
  await ops.getByLabel("Password").fill(PASSWORD);
  await ops.getByRole("button", { name: "Sign in to console" }).click();
  await expect(ops.getByRole("heading", { name: "Organizations" })).toBeVisible({ timeout: 30_000 });
  await ops.getByLabel("Search").fill(`smoke-acme-${TAG}`);
  await ops.getByRole("link", { name: new RegExp(`Smoke Acme ${TAG}`) }).click();
  await expect(ops.getByTestId("org-status")).toHaveText("Onboarding");

  // --- activate (reason required), then suspend with a public message and a private note
  await ops.getByRole("button", { name: "Activate", exact: true }).click();
  await ops.getByRole("dialog").getByRole("button", { name: "Activate organization" }).click();
  await expect(ops.getByRole("dialog").getByText("A reason is required.")).toBeVisible();
  await ops.getByRole("dialog").getByLabel(/^Reason/).fill("contract signed");
  await ops.getByRole("dialog").getByRole("button", { name: "Activate organization" }).click();
  await expect(ops.getByTestId("org-status")).toHaveText("Active");

  await ops.getByRole("button", { name: "Suspend", exact: true }).click();
  const dialog = ops.getByRole("dialog");
  await dialog.getByLabel(/^Reason/).fill("PRIVATE-REASON smoke");
  await dialog.getByLabel(/^Message to the customer/).fill("Please contact billing.");
  await dialog.getByLabel(/^Internal note/).fill("PRIVATE-NOTE smoke");
  await dialog.getByRole("checkbox").check();
  await dialog.getByRole("button", { name: "Suspend organization" }).click();
  await expect(ops.getByTestId("org-status")).toHaveText("Suspended");
  await expect(ops.getByTestId("status-history")).toContainText("PRIVATE-NOTE smoke", { timeout: 30_000 });

  // --- the tenant admin is shut out immediately and told why (without any private operator text)
  await admin.goto("/ask");
  await expect(admin).toHaveURL(/\/suspended$/, { timeout: 30_000 });
  await expect(admin.getByTestId("suspension-message")).toHaveText("Please contact billing.");
  const page = (await admin.content()).toLowerCase();
  expect(page).not.toContain("private-note");
  expect(page).not.toContain("private-reason");
  await admin.goto("/login");
  await admin.getByLabel("Work email").fill(ADMIN);
  await admin.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await admin.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(admin).toHaveURL(/\/suspended$/); // cannot sign in either

  // --- reactivate
  await ops.getByRole("button", { name: "Reactivate", exact: true }).click();
  await ops.getByRole("dialog").getByLabel(/^Reason/).fill("paid");
  await ops.getByRole("dialog").getByRole("button", { name: "Reactivate organization" }).click();
  await expect(ops.getByTestId("org-status")).toHaveText("Active");
  await expect(ops.getByTestId("status-history")).toContainText("paid", { timeout: 30_000 });

  // --- the admin signs in again and works
  await admin.goto("/login");
  await admin.getByLabel("Work email").fill(ADMIN);
  await admin.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await admin.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(admin).toHaveURL(/\/ask$/, { timeout: 30_000 });
  await admin.goto("/settings/organization");
  await expect(admin.getByTestId("organization-status")).toHaveText("Active");

  await opsContext.close();
  await adminContext.close();
});

test("a tenant administrator cannot use the console", async ({ page }) => {
  await page.goto("/platform/login");
  await page.getByLabel("Operator email").fill(ADMIN);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in to console" }).click();
  await expect(page.getByRole("alert")).toHaveText("Invalid email or password.");
  await expect(page).toHaveURL(/\/platform\/login$/);
});
