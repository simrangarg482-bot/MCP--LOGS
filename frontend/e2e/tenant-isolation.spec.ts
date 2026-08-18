import { test, expect } from "@playwright/test";
import { API_BASE_URL, apiLogin, decodeOrganizationId, loginViaUI } from "./fixtures/actions";
import { ORG_ALPHA, ORG_BETA } from "./fixtures/testUsers";

/**
 * Org Alpha and Org Beta (both seeded by `scripts/e2e_seed.py`) are
 * genuinely separate `organizations` rows with their own admin users --
 * this proves neither can see the other's data through the real UI or by
 * directly attempting the other org's API calls with its own valid session.
 */
test.describe("Tenant isolation", () => {
  test("an incident created in Org Alpha does not appear in Org Beta's incident list", async ({ page }) => {
    await loginViaUI(page, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);
    const title = `Tenant isolation canary ${Date.now()}`;
    await page.goto("/incidents/new");
    await page.getByLabel("Title").fill(title);
    await page.getByLabel("Description").fill("Created in Org Alpha; must never be visible from Org Beta.");
    await page.getByRole("button", { name: "Create incident" }).click();
    await expect(page).toHaveURL(/\/incidents\/[0-9a-f-]+$/, { timeout: 15_000 });

    await page.context().clearCookies();
    await loginViaUI(page, ORG_BETA.admin.email, ORG_BETA.admin.password);
    await page.goto("/incidents");
    await expect(page.getByText(title)).toHaveCount(0);
  });

  test("Org Beta's admin cannot fetch an Org Alpha incident by ID directly", async ({ page, request }) => {
    await loginViaUI(page, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);
    await page.goto("/incidents/new");
    const title = `Cross-org direct-fetch canary ${Date.now()}`;
    await page.getByLabel("Title").fill(title);
    await page.getByLabel("Description").fill("Org Alpha incident; Org Beta must get 404, not the real data.");
    await page.getByRole("button", { name: "Create incident" }).click();
    await expect(page).toHaveURL(/\/incidents\/([0-9a-f-]+)$/, { timeout: 15_000 });
    const incidentId = page.url().split("/incidents/")[1];

    const betaToken = await apiLogin(request, ORG_BETA.admin.email, ORG_BETA.admin.password);
    const response = await request.get(`${API_BASE_URL}/incidents/${incidentId}`, {
      headers: { Authorization: `Bearer ${betaToken}` },
      failOnStatusCode: false,
    });
    expect(response.status()).toBe(404);
  });

  test("connectors registered in one org never appear in the other org's connector list", async ({ request }) => {
    const alphaToken = await apiLogin(request, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);
    const betaToken = await apiLogin(request, ORG_BETA.admin.email, ORG_BETA.admin.password);

    const alphaConnectors = await request.get(`${API_BASE_URL}/tenancy/connectors`, {
      headers: { Authorization: `Bearer ${alphaToken}` },
    });
    const betaConnectors = await request.get(`${API_BASE_URL}/tenancy/connectors`, {
      headers: { Authorization: `Bearer ${betaToken}` },
    });
    const alphaIds = new Set((await alphaConnectors.json()).map((c: { id: string }) => c.id));
    const betaIds = new Set((await betaConnectors.json()).map((c: { id: string }) => c.id));
    for (const id of alphaIds) expect(betaIds.has(id)).toBe(false);
  });

  test("audit events recorded in one org never appear in the other org's audit log", async ({ page, request }) => {
    await loginViaUI(page, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);
    await page.goto("/incidents/new");
    await page.getByLabel("Title").fill(`Audit isolation canary ${Date.now()}`);
    await page.getByLabel("Description").fill("Creating this records a real incident.create audit event in Org Alpha only.");
    await page.getByRole("button", { name: "Create incident" }).click();
    await expect(page).toHaveURL(/\/incidents\/([0-9a-f-]+)$/, { timeout: 15_000 });
    const incidentId = page.url().split("/incidents/")[1];

    const betaToken = await apiLogin(request, ORG_BETA.admin.email, ORG_BETA.admin.password);
    const betaOrgId = decodeOrganizationId(betaToken);
    // `resource_id` is `AuditLogQuery`'s own filter field -- if tenant
    // scoping ever broke, this would return Org Alpha's real audit row for
    // an id Org Beta's admin has no business matching against.
    const betaAudit = await request.get(
      `${API_BASE_URL}/organizations/${betaOrgId}/audit?resource_id=${incidentId}`,
      { headers: { Authorization: `Bearer ${betaToken}` } },
    );
    const betaEntries: unknown[] = await betaAudit.json();
    expect(betaEntries).toHaveLength(0);
  });
});
