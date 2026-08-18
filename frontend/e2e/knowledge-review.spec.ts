import { test, expect } from "@playwright/test";
import { loginViaUI, apiLogin, API_BASE_URL } from "./fixtures/actions";
import { ORG_ALPHA } from "./fixtures/testUsers";

/**
 * `scripts/e2e_seed.py` seeds exactly one proposed document in Org Alpha
 * (`PROPOSED_DOCUMENT_TITLE`) -- `propose_document` is MCP/agent-only, not
 * REST-exposed, so there's no way for this spec to create its own the way
 * `incidents.spec.ts`-style specs create their own incidents. Both tests
 * below act on that single seeded document; running this file more than
 * once against the same seeded database without re-seeding will find zero
 * proposed documents on the second run (the first run consumes it via
 * reject or publish) -- expected, not a flake, given seeding is a one-shot
 * fixture, not idempotent-per-test-run.
 */
const SEEDED_DOCUMENT_TITLE = "E2E Seeded Proposal: Checkout Retry Runbook";

test.describe("Knowledge Review", () => {
  test("a restricted user (no knowledge:review) does not see the nav item and is denied server-side", async ({
    page,
    request,
  }) => {
    await loginViaUI(page, ORG_ALPHA.restricted.email, ORG_ALPHA.restricted.password);

    await expect(page.getByRole("link", { name: "Knowledge Review" })).toHaveCount(0);

    // Server-side enforcement, independent of the UI: `GET /knowledge/proposed`
    // requires `knowledge:review` at the service layer
    // (`app.core.knowledge.service.list_proposed_documents`).
    const accessToken = await apiLogin(request, ORG_ALPHA.restricted.email, ORG_ALPHA.restricted.password);
    const response = await request.get(`${API_BASE_URL}/knowledge/proposed`, {
      headers: { Authorization: `Bearer ${accessToken}` },
      failOnStatusCode: false,
    });
    expect(response.status()).toBe(403);
  });

  test("an admin can see, and reject, the seeded proposed document", async ({ page }) => {
    await loginViaUI(page, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);

    await page.getByRole("link", { name: "Knowledge Review" }).click();
    await expect(page).toHaveURL(/\/knowledge\/review$/);

    const card = page.getByText(SEEDED_DOCUMENT_TITLE).locator("..").locator("..");
    await expect(card).toBeVisible({ timeout: 15_000 });
    await expect(card.getByText("Proposed")).toBeVisible();

    await card.getByRole("button", { name: "Reject" }).click();
    await page.getByRole("button", { name: "Reject", exact: true }).last().click(); // confirm dialog

    // A rejected document is soft-deleted server-side and disappears from
    // the review queue entirely -- there is no "rejected" status to assert
    // instead (see `app.core.knowledge.service.reject_document`'s own
    // docstring: `status` stays "proposed" on the now-invisible row).
    await expect(page.getByText(SEEDED_DOCUMENT_TITLE)).toHaveCount(0, { timeout: 15_000 });
    await expect(page.getByText(/rejected/i)).toBeVisible();
  });
});
