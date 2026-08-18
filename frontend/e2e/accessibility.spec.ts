import { test, expect } from "@playwright/test";
import { loginViaUI } from "./fixtures/actions";
import { ORG_ALPHA } from "./fixtures/testUsers";

/**
 * Functional accessibility checks -- actually driving focus/keyboard through
 * the real DOM, not static inspection. Complements responsive.spec.ts (which
 * already exercises the mobile nav overlay's own focus trap/restore) by
 * covering the shared `Modal` component, toast alert semantics, and
 * label/accessible-name coverage on real forms and icon-only buttons.
 */
test.describe("Accessibility", () => {
  test("Modal receives focus, traps Tab within it, and restores focus to the trigger on close", async ({
    page,
  }) => {
    await loginViaUI(page, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);
    await page.goto("/connectors");

    const trigger = page.getByRole("button", { name: "Connect a source" });
    await trigger.click();

    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    // Focus moves into the dialog on open (not left behind on the trigger).
    await expect(dialog.locator(":focus")).toHaveCount(1);

    // Shift+Tab from the first focusable element should wrap to the last
    // one inside the dialog, not escape to the page behind it.
    await page.keyboard.press("Shift+Tab");
    await expect(dialog.locator(":focus")).toHaveCount(1);

    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(trigger).toBeFocused();
  });

  test("a failed action surfaces an accessible alert", async ({ page }) => {
    await page.goto("/login");
    await page.getByLabel("Work email").fill(ORG_ALPHA.admin.email);
    await page.getByLabel("Password").fill("wrong-password-on-purpose");
    await page.getByRole("button", { name: "Sign in" }).click();
    // ToastContext renders error/warning toasts with role="alert" +
    // aria-live="assertive" -- a screen reader is interrupted, not just a
    // sighted user seeing a box appear.
    await expect(page.getByRole("alert")).toBeVisible({ timeout: 10_000 });
  });

  test("login form inputs have accessible labels and the submit button has an accessible name", async ({
    page,
  }) => {
    await page.goto("/login");
    await expect(page.getByLabel("Work email")).toBeVisible();
    await expect(page.getByLabel("Password")).toBeVisible();
    await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();

    // Keyboard-only flow: Tab from the email field should reach the
    // password field, then the submit button, in document order.
    await page.getByLabel("Work email").focus();
    await page.keyboard.press("Tab");
    await expect(page.getByLabel("Password")).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("button", { name: "Sign in" })).toBeFocused();
  });

  test("icon-only controls expose accessible names", async ({ page }) => {
    await loginViaUI(page, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);

    // Topbar/account menu and the incident timeline's icon-only affordances
    // are otherwise unlabeled glyphs -- getByRole with a name only succeeds
    // if an aria-label (or equivalent) is actually present.
    await expect(page.getByRole("button", { name: /Account menu/ })).toBeVisible();

    await page.goto("/connectors");
    await page.getByRole("button", { name: "Connect a source" }).click();
    await expect(page.getByRole("button", { name: "Close dialog" })).toBeVisible();
  });
});
