import { expect, test, type Page } from "@playwright/test";

/**
 * Outcome-feedback controls on the Ask page.
 *
 * Runs against the frontend's built-in MOCK data layer (start Vite with
 * `VITE_USE_MOCK_DATA=true`): the mock login accepts any credentials and the
 * mock feedback API is an in-memory store with a 400 ms latency (so the
 * loading state is observable) that answers 404 for a deliberately
 * "unrateable" execution. No backend, database or network is involved --
 * which is what makes the interaction rules testable deterministically. The
 * real wire contract is covered by `tests/api/test_feedback_router.py`
 * and `feedback-helpers.unit.spec.ts` (request body casing).
 */

async function signInAndOpenAsk(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Work email").fill("tester@example.test");
  await page.getByLabel("Password").fill("not-a-real-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  // Do NOT `page.goto("/ask")`: a full reload would drop the in-memory mock
  // session. A successful login already lands on `/` -> `/ask`.
  await expect(page).toHaveURL(/\/ask$/, { timeout: 15_000 });
}

async function ask(page: Page, question: string): Promise<void> {
  const composer = page.getByLabel("Question for EKIP");
  await composer.fill(question);
  await composer.press("Enter");
}

test.describe("Ask EKIP - outcome feedback", () => {
  test.beforeEach(async ({ page }) => {
    await signInAndOpenAsk(page);
  });

  test("an answer shows Helpful and Not helpful, nothing pre-selected", async ({ page }) => {
    await ask(page, "How does the promo code engine work?");

    const feedback = page.getByTestId("answer-feedback");
    await expect(feedback).toBeVisible({ timeout: 15_000 });
    await expect(feedback.getByRole("button", { name: "Helpful", exact: true })).toHaveAttribute("aria-pressed", "false");
    await expect(feedback.getByRole("button", { name: "Not helpful" })).toHaveAttribute("aria-pressed", "false");
    // The normal answer UI is undisturbed.
    await expect(page.getByRole("button", { name: /Copy answer/ })).toBeVisible();
  });

  test("Helpful: shows saving, then the saved state, with no explanation asked for", async ({ page }) => {
    await ask(page, "How does the promo code engine work?");
    const feedback = page.getByTestId("answer-feedback");
    await expect(feedback).toBeVisible({ timeout: 15_000 });

    await feedback.getByRole("button", { name: "Helpful", exact: true }).click();

    await expect(feedback.getByText("Saving…")).toBeVisible();
    await expect(feedback.getByText("Thanks — feedback saved")).toBeVisible();
    await expect(feedback.getByRole("button", { name: "Helpful", exact: true })).toHaveAttribute("aria-pressed", "true");
    await expect(feedback.getByRole("button", { name: "Not helpful" })).toHaveAttribute("aria-pressed", "false");
    await expect(feedback.getByPlaceholder(/Tell us more/)).toHaveCount(0);
  });

  test("buttons are disabled while saving, so a double click cannot submit twice", async ({ page }) => {
    await ask(page, "How does the promo code engine work?");
    const feedback = page.getByTestId("answer-feedback");
    await expect(feedback).toBeVisible({ timeout: 15_000 });

    const helpful = feedback.getByRole("button", { name: "Helpful", exact: true });
    await helpful.click();

    await expect(feedback.getByText("Saving…")).toBeVisible();
    await expect(helpful).toBeDisabled();
    await expect(feedback.getByRole("button", { name: "Not helpful" })).toBeDisabled();
    await expect(feedback.getByText("Thanks — feedback saved")).toBeVisible();
    // Re-clicking the saved rating changes nothing and starts no new save.
    await helpful.click();
    await expect(feedback.getByText("Saving…")).toHaveCount(0);
    await expect(helpful).toHaveAttribute("aria-pressed", "true");
  });

  test("Not helpful saves immediately and only OFFERS an explanation, which can be skipped", async ({ page }) => {
    await ask(page, "How does the promo code engine work?");
    const feedback = page.getByTestId("answer-feedback");
    await expect(feedback).toBeVisible({ timeout: 15_000 });

    await feedback.getByRole("button", { name: "Not helpful" }).click();

    await expect(feedback.getByText("Thanks — feedback saved")).toBeVisible();
    await expect(feedback.getByRole("button", { name: "Not helpful" })).toHaveAttribute("aria-pressed", "true");
    await expect(feedback.getByText("What went wrong? (optional)")).toBeVisible();

    await feedback.getByRole("button", { name: "Skip" }).click();

    await expect(feedback.getByText("What went wrong? (optional)")).toHaveCount(0);
    // The rating is kept, and details can still be added later.
    await expect(feedback.getByRole("button", { name: "Not helpful" })).toHaveAttribute("aria-pressed", "true");
    await expect(feedback.getByRole("button", { name: "Add details (optional)" })).toBeVisible();
  });

  test("a reason and comment can be added to a negative rating", async ({ page }) => {
    await ask(page, "How does the promo code engine work?");
    const feedback = page.getByTestId("answer-feedback");
    await expect(feedback).toBeVisible({ timeout: 15_000 });
    await feedback.getByRole("button", { name: "Not helpful" }).click();
    await expect(feedback.getByText("What went wrong? (optional)")).toBeVisible();

    await feedback.getByRole("button", { name: "Out of date" }).click();
    await expect(feedback.getByRole("button", { name: "Out of date" })).toHaveAttribute("aria-pressed", "true");
    await feedback.getByPlaceholder(/Tell us more/).fill("This describes v1.");
    await feedback.getByRole("button", { name: "Send details" }).click();

    await expect(feedback.getByText("Thanks — details saved")).toBeVisible();
    await expect(feedback.getByRole("button", { name: "Edit details" })).toBeVisible();
  });

  test("a person can change their mind: the new rating replaces the old one", async ({ page }) => {
    await ask(page, "How does the promo code engine work?");
    const feedback = page.getByTestId("answer-feedback");
    await expect(feedback).toBeVisible({ timeout: 15_000 });

    await feedback.getByRole("button", { name: "Helpful", exact: true }).click();
    await expect(feedback.getByText("Thanks — feedback saved")).toBeVisible();
    await feedback.getByRole("button", { name: "Not helpful" }).click();

    await expect(feedback.getByRole("button", { name: "Not helpful" })).toHaveAttribute("aria-pressed", "true");
    await expect(feedback.getByRole("button", { name: "Helpful", exact: true })).toHaveAttribute("aria-pressed", "false");
  });

  test("two answers in one conversation are rated independently", async ({ page }) => {
    await ask(page, "First question about promo codes");
    await expect(page.getByTestId("answer-feedback")).toHaveCount(1, { timeout: 15_000 });
    await ask(page, "Second question about promo codes");
    await expect(page.getByTestId("answer-feedback")).toHaveCount(2, { timeout: 15_000 });

    const [first, second] = [page.getByTestId("answer-feedback").nth(0), page.getByTestId("answer-feedback").nth(1)];
    await first.getByRole("button", { name: "Helpful", exact: true }).click();
    await expect(first.getByText("Thanks — feedback saved")).toBeVisible();

    await expect(first.getByRole("button", { name: "Helpful", exact: true })).toHaveAttribute("aria-pressed", "true");
    await expect(second.getByRole("button", { name: "Helpful", exact: true })).toHaveAttribute("aria-pressed", "false");
    await expect(second.getByRole("button", { name: "Not helpful" })).toHaveAttribute("aria-pressed", "false");
  });

  test("a failed save shows a clear error, keeps nothing selected, and does not offer retry for a 404", async ({
    page,
  }) => {
    await ask(page, "This one is unrateable");
    const feedback = page.getByTestId("answer-feedback");
    await expect(feedback).toBeVisible({ timeout: 15_000 });

    await feedback.getByRole("button", { name: "Helpful", exact: true }).click();

    await expect(feedback.getByRole("alert")).toContainText("can no longer be rated");
    await expect(feedback.getByRole("button", { name: "Helpful", exact: true })).toHaveAttribute("aria-pressed", "false");
    await expect(feedback.getByRole("button", { name: "Retry" })).toHaveCount(0);
    // The answer itself is unaffected.
    await expect(page.getByRole("button", { name: /Copy answer/ })).toBeVisible();
  });

  test("the comment box refuses text past the limit and says nothing is required", async ({ page }) => {
    await ask(page, "How does the promo code engine work?");
    const feedback = page.getByTestId("answer-feedback");
    await expect(feedback).toBeVisible({ timeout: 15_000 });
    await feedback.getByRole("button", { name: "Not helpful" }).click();

    const box = feedback.getByPlaceholder(/Tell us more/);
    await expect(box).toHaveAttribute("maxlength", "2000");
    await expect(box).toHaveAttribute("placeholder", /passwords or keys/);
    // "Send details" works with nothing filled in -- no explanation is forced.
    await expect(feedback.getByRole("button", { name: "Send details" })).toBeEnabled();
  });
});
