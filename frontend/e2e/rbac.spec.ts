import { test, expect } from "@playwright/test";
import { API_BASE_URL, apiLogin, decodeOrganizationId, loginViaUI } from "./fixtures/actions";
import { ORG_ALPHA } from "./fixtures/testUsers";

/**
 * `e2e-restricted@example.com` (seeded by `scripts/e2e_seed.py`) holds a
 * role granting only `incident:write` + `incident:read` in Org Alpha --
 * every other permission code the backend checks is deliberately absent, so
 * every assertion below is "has these, lacks the rest" on a single real
 * account rather than one narrow single-permission fixture per code.
 * `incident:read` is included alongside `incident:write` so this user can
 * view the incident the "can create an incident" test below creates --
 * without it, Phase 4.7.2's incident-read authorization fix would deny that
 * read with the same account that was just allowed to write it.
 */
test.describe("RBAC", () => {
  test("a restricted user's nav hides every permission-gated item they lack", async ({ page }) => {
    await loginViaUI(page, ORG_ALPHA.restricted.email, ORG_ALPHA.restricted.password);
    await expect(page).toHaveURL(/\/ask$/);

    // Ungated items: always visible.
    await expect(page.getByRole("link", { name: "Ask EKIP" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Incidents" })).toBeVisible();

    // Gated items this user lacks the permission for: must not render at all.
    await expect(page.getByRole("link", { name: "Connectors" })).toHaveCount(0); // tenancy:manage
    await expect(page.getByRole("link", { name: "Audit Log" })).toHaveCount(0); // audit:read
    await expect(page.getByRole("link", { name: "Agents" })).toHaveCount(0); // observability:read
    await expect(page.getByRole("link", { name: "MCP Tools" })).toHaveCount(0); // observability:read
    await expect(page.getByRole("link", { name: "Knowledge Gaps" })).toHaveCount(0); // knowledge:review
  });

  test("an admin's nav shows every permission-gated item they hold", async ({ page }) => {
    await loginViaUI(page, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);
    await expect(page.getByRole("link", { name: "Connectors" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Audit Log" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Agents" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Knowledge Gaps" })).toBeVisible();
  });

  test("a restricted user cannot open the Settings > Connectors tab (hidden), and the Connectors page action is disabled", async ({
    page,
  }) => {
    await loginViaUI(page, ORG_ALPHA.restricted.email, ORG_ALPHA.restricted.password);
    await page.goto("/settings");
    await expect(page.getByRole("link", { name: "Connectors", exact: true })).toHaveCount(0);
  });

  test("a restricted user can create an incident and view it back", async ({ page }) => {
    await loginViaUI(page, ORG_ALPHA.restricted.email, ORG_ALPHA.restricted.password);
    await page.goto("/incidents/new");
    const title = `RBAC restricted-user incident ${Date.now()}`;
    await page.getByLabel("Title").fill(title);
    await page.getByLabel("Description").fill("Created by the restricted RBAC test user to prove incident:write works.");
    await page.getByRole("button", { name: "Create incident" }).click();
    await expect(page).toHaveURL(/\/incidents\/[0-9a-f-]+$/, { timeout: 15_000 });
    await expect(page.getByRole("heading", { name: title })).toBeVisible();
  });

  test("a restricted user directly attempting permission-gated API calls is denied by the backend, not just the UI", async ({
    request,
  }) => {
    // The access token lives only in the running app's memory (tokenStore.ts
    // deliberately never persists it), so this logs in again through the
    // same real `POST /auth/login` the UI itself calls -- proving each
    // denial is enforced server-side (`require_permission`), independent of
    // whatever the frontend nav chooses to render (already proven separately
    // above).
    const accessToken = await apiLogin(request, ORG_ALPHA.restricted.email, ORG_ALPHA.restricted.password);
    const authHeader = { Authorization: `Bearer ${accessToken}` };
    const organizationId = decodeOrganizationId(accessToken);

    const connectors = await request.get(`${API_BASE_URL}/tenancy/connectors`, {
      headers: authHeader,
      failOnStatusCode: false,
    });
    expect(connectors.status()).toBe(403); // tenancy:manage

    const audit = await request.get(`${API_BASE_URL}/organizations/${organizationId}/audit`, {
      headers: authHeader,
      failOnStatusCode: false,
    });
    expect(audit.status()).toBe(403); // audit:read

    const agentStats = await request.get(`${API_BASE_URL}/observability/agents`, {
      headers: authHeader,
      failOnStatusCode: false,
    });
    expect(agentStats.status()).toBe(403); // observability:read

    const sso = await request.get(`${API_BASE_URL}/organizations/${organizationId}/sso`, {
      headers: authHeader,
      failOnStatusCode: false,
    });
    expect(sso.status()).toBe(403); // tenancy:manage
  });

  test("an admin can view the real SSO configuration through Settings > SSO", async ({ page, request }) => {
    // Self-contained: configures SSO via the real API first (tolerating a
    // 409 if an earlier run already did) rather than depending on some
    // other test or manual setup having configured it -- `configure_sso` is
    // create-only, not an upsert (see its own docstring), so this is the
    // one endpoint in this file that can't just be called unconditionally.
    const accessToken = await apiLogin(request, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);
    const organizationId = decodeOrganizationId(accessToken);
    await request.post(`${API_BASE_URL}/organizations/${organizationId}/sso/configure`, {
      headers: { Authorization: `Bearer ${accessToken}` },
      data: {
        provider: "okta",
        issuer_url: "https://acme.okta.com",
        client_id: "e2e-client-id",
        client_secret_ref: "e2e-plaintext-secret-never-shown-back",
      },
      failOnStatusCode: false, // 201 first run, 409 on every later run -- both fine
    });

    await loginViaUI(page, ORG_ALPHA.admin.email, ORG_ALPHA.admin.password);
    await page.goto("/settings/sso");
    // Real backend data, not a mock: whatever provider/issuer this org was
    // actually configured with, plus the secret always coming back redacted
    // (see `core.tenancy.service._redact_client_secret`) -- never blank
    // (which would also be true of a totally broken fetch) and never the
    // literal plaintext this org's SSO was configured with. A generous
    // timeout: unlike every other page in this suite, this one chains THREE
    // sequential real round trips before it can render anything --
    // TenantContext's own `listOrganizations`, then `listProjects`, then
    // this page's own `getSsoConfig` -- each its own RLS-scoped query
    // against this environment's real remote dev database.
    await expect(page.getByLabel("Issuer URL")).not.toHaveValue("", { timeout: 30_000 });
    await expect(page.getByLabel("Client secret reference")).toHaveValue("••••••••");
  });
});
