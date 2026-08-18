import { test, expect } from "@playwright/test";
import { apiLogin, decodeOrganizationId, API_BASE_URL } from "./fixtures/actions";
import { ORG_ALPHA } from "./fixtures/testUsers";

/**
 * Exercises the real Phase 7.5 flow end-to-end: `POST /organizations/{id}/
 * invitations` (`core.tenancy.service.create_invitation`) returns a raw,
 * single-use `token` exactly once; `POST /invitations/{id}/accept`
 * (`core.auth.service.accept_invitation_with_password`) provisions a real
 * user from it and returns a session. This spec drives invitation creation
 * via the real REST API (no UI for it exists to seed from otherwise -- see
 * `UsersSettingsPage`'s "Invite user" modal for the one place a human would
 * do this) and drives *acceptance* through the actual `AcceptInvitationPage`
 * UI, since that's the surface being verified.
 */
test.describe("Accept invitation", () => {
  test("a fresh invitation link provisions a real user and logs them in", async ({ page, request }) => {
    const accessToken = await apiLogin(request, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);
    const organizationId = decodeOrganizationId(accessToken);
    const email = `e2e-invitee-${Date.now()}@example.com`;

    const createResponse = await request.post(`${API_BASE_URL}/organizations/${organizationId}/invitations`, {
      headers: { Authorization: `Bearer ${accessToken}` },
      data: { email, grants_role: "member" },
    });
    expect(createResponse.ok(), `create_invitation failed: ${createResponse.status()}`).toBeTruthy();
    const invitation = await createResponse.json();
    expect(invitation.token).toBeTruthy();

    await page.goto(`/invitations/${invitation.id}/accept?token=${invitation.token}`);
    await page.getByLabel("Your name").fill("E2E Invitee");
    await page.getByLabel("Choose a password").fill("AcceptedInvite123!");
    await page.getByRole("button", { name: "Accept invitation" }).click();

    await expect(page).not.toHaveURL(/\/invitations\//, { timeout: 15_000 });

    // A real, distinct session for the newly-provisioned user -- not a
    // leftover admin session and not a no-op.
    const meResponse = await page.request.get(`${API_BASE_URL}/auth/me`);
    expect(meResponse.ok()).toBeTruthy();
    const me = await meResponse.json();
    expect(me.email).toBe(email);
  });

  test("re-using an already-accepted invitation's link is rejected", async ({ page, request }) => {
    const accessToken = await apiLogin(request, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);
    const organizationId = decodeOrganizationId(accessToken);
    const email = `e2e-invitee-reuse-${Date.now()}@example.com`;

    const createResponse = await request.post(`${API_BASE_URL}/organizations/${organizationId}/invitations`, {
      headers: { Authorization: `Bearer ${accessToken}` },
      data: { email, grants_role: "member" },
    });
    const invitation = await createResponse.json();

    const firstAccept = await request.post(`${API_BASE_URL}/invitations/${invitation.id}/accept`, {
      data: { token: invitation.token, password: "AcceptedInvite123!" },
    });
    expect(firstAccept.ok()).toBeTruthy();

    await page.goto(`/invitations/${invitation.id}/accept?token=${invitation.token}`);
    await page.getByLabel("Choose a password").fill("AnotherPassword123!");
    await page.getByRole("button", { name: "Accept invitation" }).click();

    await expect(page.getByText(/couldn.t accept invitation/i)).toBeVisible({ timeout: 15_000 });
    await expect(page).toHaveURL(/\/invitations\//);
  });
});
