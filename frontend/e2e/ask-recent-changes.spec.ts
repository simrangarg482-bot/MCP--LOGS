import { test, expect } from "@playwright/test";
import { loginViaUI } from "./fixtures/actions";
import { ORG_ALPHA } from "./fixtures/testUsers";

test.describe("Ask EKIP - Recent changes quick action", () => {
  test("clicking Recent changes no longer sends the hardcoded payments-service question", async ({ page }) => {
    await loginViaUI(page, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);
    await page.goto("/ask");

    // Intercept the real `POST /search/recent-changes` call instead of
    // letting it reach the backend -- this test only cares what the
    // frontend sends, not what comes back, so it fulfils with an empty
    // result set (a valid `list[ScoredChunk]` response) rather than
    // depending on real ingested commit/PR/issue history existing.
    let requestBody: { query?: string; collection?: string } | undefined;
    await page.route("**/search/recent-changes", async (route) => {
      requestBody = route.request().postDataJSON();
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
    });

    await page.getByRole("button", { name: /Recent changes/ }).click();

    await expect.poll(() => requestBody?.query).not.toBeUndefined();
    // The actual regression: this used to always be the hardcoded
    // "What changed recently in the payments service?" (see
    // `AskPage.tsx`'s `STARTER_QUESTIONS` entry for `action: "recent_changes"`),
    // regardless of what the user wanted to know about.
    expect(requestBody?.query).not.toMatch(/payments service/i);
    // Everything else about the request -- the collection selection in
    // particular -- must be unaffected by this fix.
    expect(requestBody?.collection).toBe("documentation");
  });
});
