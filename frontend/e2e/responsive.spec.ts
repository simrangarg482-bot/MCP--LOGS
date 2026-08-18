import { test, expect, devices } from "@playwright/test";
import { loginViaUI } from "./fixtures/actions";
import { ORG_ALPHA } from "./fixtures/testUsers";

const VIEWPORTS = {
  desktop: { width: 1440, height: 900 },
  tablet: { width: 834, height: 1112 },
  mobile: devices["iPhone 13"].viewport,
};

test.describe("Responsive layout", () => {
  test("desktop: persistent sidebar, no hamburger", async ({ page }) => {
    await page.setViewportSize(VIEWPORTS.desktop);
    await loginViaUI(page, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);
    await expect(page.getByRole("link", { name: "Ask EKIP" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Toggle sidebar" })).toBeHidden();
  });

  test("tablet: sidebar collapses behind the hamburger", async ({ page }) => {
    await page.setViewportSize(VIEWPORTS.tablet);
    await loginViaUI(page, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);
    await expect(page.getByRole("button", { name: "Toggle sidebar" })).toBeVisible();
    // The persistent (lg+) sidebar copy is hidden at this width -- only the
    // off-canvas one (opened below) should be reachable.
    await expect(page.getByRole("navigation")).toBeHidden();
  });

  test("mobile: hamburger opens the nav as a real overlay, traps focus, and restores focus on close", async ({
    page,
  }) => {
    await page.setViewportSize(VIEWPORTS.mobile);
    await loginViaUI(page, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);

    const hamburger = page.getByRole("button", { name: "Toggle sidebar" });
    await expect(hamburger).toBeVisible();
    await hamburger.click();

    const nav = page.getByRole("dialog", { name: "Navigation menu" });
    await expect(nav).toBeVisible();
    // Focus should have moved into the overlay (the mobile nav fix this
    // regression-tests: the hamburger previously toggled a `collapsed`
    // state that `isTablet` was already forcing, so the button had no
    // visible effect at all).
    await expect(nav.locator(":focus")).toHaveCount(1);

    await page.keyboard.press("Escape");
    await expect(nav).toBeHidden();
    await expect(hamburger).toBeFocused();

    // Tapping a nav link inside the overlay both navigates and closes it.
    await hamburger.click();
    await page.getByRole("link", { name: "Incidents" }).click();
    await expect(page).toHaveURL(/\/incidents$/);
    await expect(page.getByRole("dialog", { name: "Navigation menu" })).toBeHidden();
  });

  test("dialogs and tables stay usable on mobile", async ({ page }) => {
    await page.setViewportSize(VIEWPORTS.mobile);
    await loginViaUI(page, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);

    await page.goto("/connectors");
    await page.getByRole("button", { name: "Connect a source" }).click();
    const modal = page.getByRole("dialog");
    await expect(modal).toBeVisible();
    const box = await modal.boundingBox();
    expect(box?.width).toBeLessThanOrEqual(VIEWPORTS.mobile!.width);
    await page.getByRole("button", { name: "Close dialog" }).click();
    await expect(modal).toBeHidden();

    // The page body itself must never need horizontal scroll, even where a
    // wide table/JSON block does.
    await page.goto("/audit");
    const bodyScrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
    const viewportWidth = await page.evaluate(() => window.innerWidth);
    expect(bodyScrollWidth).toBeLessThanOrEqual(viewportWidth + 1);
  });
});
