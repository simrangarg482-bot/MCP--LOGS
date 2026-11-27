import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * UI tests for emailed invitations: delivery status after inviting, the pending
 * list's status badge, Resend / Copy new link, duplicate-click protection, and
 * the accept page's fragment-or-query token handling.
 *
 * Like `roles-ui.spec.ts` this answers every backend call with a small stateful
 * fake (`page.route`), so it needs only the Vite dev server built with
 * `VITE_USE_MOCK_DATA=false VITE_API_BASE_URL=http://127.0.0.1:8010`. The rules
 * themselves (caps, cooldown, token rotation, RLS, background delivery) are
 * enforced and tested server-side; this checks what the UI does with the
 * contract and its error responses.
 */

const API = process.env.E2E_API_BASE_URL ?? "http://127.0.0.1:8010";
const ORG = "11111111-1111-4111-8111-111111111111";
const ME = "22222222-2222-4222-8222-222222222222";

interface FakeInvitation {
  id: string;
  organization_id: string;
  email: string;
  status: "pending";
  grants_role_id: string;
  invited_by: string;
  expires_at: string;
  accepted_at: null;
  created_at: string;
  updated_at: string;
  email_status: string;
  email_attempts: number;
  email_sent_at: string | null;
  resend_count: number;
  token?: string | null;
  pinned?: boolean;
}

interface Options {
  emailConfigured: boolean;
  /** statuses the created invitation reports on successive list calls */
  deliverySequence?: string[];
  resendError?: { status: number; error_code: string; message: string; detail?: unknown };
  resendDelayMs?: number;
}

function makeBackend(options: Options) {
  const invitations: FakeInvitation[] = [];
  const calls: Array<{ method: string; path: string; body: unknown }> = [];
  let listCalls = 0;
  const sequence = options.deliverySequence ?? ["queued", "sent"];

  const json = (route: Route, status: number, body: unknown) =>
    route.fulfill({ status, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(body) });

  const make = (email: string, token: string | null, emailStatus: string): FakeInvitation => ({
    id: `inv-${invitations.length + 1}`,
    organization_id: ORG,
    email,
    status: "pending",
    grants_role_id: "r-viewer",
    invited_by: ME,
    expires_at: "2099-01-01T00:00:00Z",
    accepted_at: null,
    created_at: "2026-11-26T00:00:00Z",
    updated_at: "2026-11-26T00:00:00Z",
    email_status: emailStatus,
    email_attempts: 0,
    email_sent_at: null,
    resend_count: 0,
    token,
  });

  async function handle(route: Route) {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const method = req.method();
    if (method === "OPTIONS") {
      return route.fulfill({
        status: 204,
        headers: {
          "access-control-allow-origin": "*",
          "access-control-allow-headers": "*",
          "access-control-allow-methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
        },
      });
    }
    const body = req.postData() ? JSON.parse(req.postData()!) : undefined;
    calls.push({ method, path, body });

    if (path === "/auth/refresh") {
      const claims = Buffer.from(JSON.stringify({ sub: ME, organization_id: ORG, exp: 4102444800 })).toString("base64url");
      return json(route, 200, { access_token: `h.${claims}.s`, refresh_token: "rt2", token_type: "bearer", expires_in: 3600 });
    }
    if (path === "/auth/me") {
      return json(route, 200, { id: ME, email: "me@example.com", display_name: "Me Admin", is_active: true, roles: ["admin"], permissions: ["tenancy:manage", "incident:read", "observability:read"] });
    }
    if (path === "/auth/organizations") return json(route, 200, { organizations: [{ id: ORG, name: "Acme", slug: "acme" }] });
    if (path === "/organizations") return json(route, 200, [{ id: ORG, name: "Acme", slug: "acme" }]);
    if (path === `/organizations/${ORG}/projects`) return json(route, 200, []);
    if (path === `/organizations/${ORG}/members`) {
      return json(route, 200, [{ id: ME, email: "me@example.com", display_name: "Me Admin", is_active: true, roles: ["admin"], role_ids: ["r-admin"], created_at: "2026-01-01T00:00:00Z" }]);
    }
    if (path === `/organizations/${ORG}/permissions`) {
      return json(route, 200, [
        { code: "incident:read", label: "View incidents", description: "See incidents.", group: "Incidents" },
        { code: "observability:read", label: "View observability", description: "See health.", group: "Observability" },
        { code: "tenancy:manage", label: "Manage organization", description: "Admin.", group: "Administration" },
      ]);
    }
    if (path === `/organizations/${ORG}/roles`) {
      return json(route, 200, [
        { id: "r-viewer", name: "viewer", description: "Read-only.", is_builtin: false, permissions: ["incident:read", "observability:read"], member_count: 0 },
      ]);
    }
    if (path === `/organizations/${ORG}/invitations` && method === "POST") {
      const created = make(body.email, options.emailConfigured ? null : "tok-manual-1", options.emailConfigured ? "queued" : "not_configured");
      invitations.unshift(created);
      return json(route, 201, created);
    }
    if (path === `/organizations/${ORG}/invitations` && method === "GET") {
      listCalls += 1;
      const first = invitations[0];
      if (first && options.emailConfigured && first.resend_count === 0 && !first.pinned) {
        const status = sequence[Math.min(listCalls - 1, sequence.length - 1)];
        first.email_status = status;
        first.email_attempts = status === "queued" ? 0 : status === "failed" ? 3 : 1;
        first.email_sent_at = status === "sent" ? new Date().toISOString() : null;
      }
      return json(route, 200, invitations.map((i) => ({ ...i, token: null })));
    }
    const resend = path.match(new RegExp(`^/organizations/${ORG}/invitations/([^/]+)/resend$`));
    if (resend && method === "POST") {
      if (options.resendDelayMs) await new Promise((r) => setTimeout(r, options.resendDelayMs));
      if (options.resendError) return json(route, options.resendError.status, options.resendError);
      const row = invitations.find((i) => i.id === resend[1])!;
      if (body.method === "email") {
        row.resend_count += 1;
        row.email_status = "queued";
        row.token = null;
      } else {
        row.email_status = "not_sent";
        row.token = "tok-fresh-2";
      }
      return json(route, 200, { ...row });
    }
    if (path.match(/^\/invitations\/[^/]+\/accept$/) && method === "POST") {
      return json(route, 202, { status: "verification_sent", message: "ok" });
    }
    return json(route, 200, []);
  }

  return { handle, calls, invitations };
}

async function open(page: Page, options: Options) {
  const backend = makeBackend(options);
  await page.addInitScript(() => localStorage.setItem("ekip.refresh_token", "rt1"));
  await page.route(`${API}/**`, backend.handle);
  return backend;
}

async function invite(page: Page, email: string) {
  await page.goto("/settings/users");
  await page.getByRole("button", { name: "Invite user" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Email").fill(email);
  await dialog.getByRole("button", { name: "Send invitation" }).click();
  return dialog;
}

test.describe("Emailed invitations", () => {
  test("shows sending, then confirms the email was accepted for delivery, with no link shown", async ({ page }) => {
    const backend = await open(page, { emailConfigured: true, deliverySequence: ["queued", "queued", "sent"] });
    const dialog = await invite(page, "new.person@example.com");

    const panel = dialog.getByTestId("invitation-email-status");
    await expect(panel).toHaveAttribute("data-status", "queued");
    await expect(panel).toContainText("Sending the invitation email to new.person@example.com");
    await expect(panel).toHaveAttribute("data-status", "sent", { timeout: 15_000 });
    await expect(panel).toContainText("accepted for delivery");
    // the raw token never reaches the browser when the server emails it
    await expect(dialog.getByTestId("invitation-link-panel")).toHaveCount(0);
    const created = backend.calls.find((c) => c.method === "POST" && c.path.endsWith("/invitations"))!;
    expect(created.body).toMatchObject({ email: "new.person@example.com", grants_role: "viewer" });
  });

  test("without an email provider the admin still gets a copyable link (fragment form)", async ({ page }) => {
    await open(page, { emailConfigured: false });
    const dialog = await invite(page, "manual@example.com");

    const panel = dialog.getByTestId("invitation-link-panel");
    await expect(panel).toContainText("Email delivery isn't configured");
    const link = await panel.getByLabel("Invitation link").inputValue();
    expect(link).toMatch(/\/invitations\/inv-1\/accept#token=tok-manual-1$/);
    expect(link).not.toContain("?token=");
  });

  test("a failed delivery says so and offers a fresh link", async ({ page }) => {
    const backend = await open(page, { emailConfigured: true, deliverySequence: ["queued", "failed"] });
    const dialog = await invite(page, "unlucky@example.com");

    const panel = dialog.getByTestId("invitation-email-status");
    await expect(panel).toHaveAttribute("data-status", "failed", { timeout: 15_000 });
    await expect(panel.getByRole("alert")).toContainText("could not be delivered");

    await panel.getByRole("button", { name: "Copy new link" }).click();
    const link = await dialog.getByLabel("Invitation link").inputValue();
    expect(link).toMatch(/#token=tok-fresh-2$/);
    expect(backend.calls.find((c) => c.path.endsWith("/resend"))!.body).toEqual({ method: "link" });
  });

  test("the pending list shows each invitation's delivery status", async ({ page }) => {
    const backend = await open(page, { emailConfigured: true, deliverySequence: ["sent"] });
    await invite(page, "listed@example.com");
    await page.getByRole("dialog").getByRole("button", { name: "Done" }).click();

    const row = page.getByRole("row", { name: /listed@example.com/ });
    await expect(row.getByTestId("email-status")).toHaveAttribute("data-status", "sent");
    await expect(row.getByText("Email sent")).toBeVisible();
    backend.invitations[0].email_status = "failed";
    backend.invitations[0].pinned = true;
    await page.reload();
    await expect(page.getByRole("row", { name: /listed@example.com/ }).getByText("Email failed")).toBeVisible();
  });

  test("Resend emails again and Copy new link shows a rotated link", async ({ page }) => {
    const backend = await open(page, { emailConfigured: true, deliverySequence: ["sent"] });
    await invite(page, "again@example.com");
    await page.getByRole("dialog").getByRole("button", { name: "Done" }).click();
    const row = page.getByRole("row", { name: /again@example.com/ });

    await row.getByRole("button", { name: "Resend" }).click();
    await expect(page.getByText("Invitation email is being sent")).toBeVisible();
    expect(backend.calls.filter((c) => c.path.endsWith("/resend")).at(-1)!.body).toEqual({ method: "email" });

    await row.getByRole("button", { name: "Copy new link" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByText("previous link no longer works")).toBeVisible();
    expect(await dialog.getByLabel("Invitation link").inputValue()).toMatch(/#token=tok-fresh-2$/);
  });

  test("the resend cooldown is explained, not shown as a generic failure", async ({ page }) => {
    await open(page, {
      emailConfigured: true,
      deliverySequence: ["sent"],
      resendError: { status: 429, error_code: "invitation.resend_cooldown", message: "wait", detail: { retry_after_seconds: 42 } },
    });
    await invite(page, "eager@example.com");
    await page.getByRole("dialog").getByRole("button", { name: "Done" }).click();
    await page.getByRole("row", { name: /eager@example.com/ }).getByRole("button", { name: "Resend" }).click();
    await expect(page.getByText("Please wait 42 seconds before re-sending this invitation.")).toBeVisible();
  });

  test("double-clicking Resend sends one request and disables the other actions meanwhile", async ({ page }) => {
    const backend = await open(page, { emailConfigured: true, deliverySequence: ["sent"], resendDelayMs: 800 });
    await invite(page, "double@example.com");
    await page.getByRole("dialog").getByRole("button", { name: "Done" }).click();
    const row = page.getByRole("row", { name: /double@example.com/ });

    await row.getByRole("button", { name: "Resend" }).dblclick();
    await expect(row.getByRole("button", { name: "Copy new link" })).toBeDisabled();
    await expect(row.getByRole("button", { name: "Revoke" })).toBeDisabled();
    await expect(page.getByText("Invitation email is being sent")).toBeVisible();
    expect(backend.calls.filter((c) => c.path.endsWith("/resend"))).toHaveLength(1);
  });

  test("a hit invitation cap is explained", async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("ekip.refresh_token", "rt1"));
    const backend = makeBackend({ emailConfigured: true });
    await page.route(`${API}/**`, async (route) => {
      const req = route.request();
      if (req.method() === "POST" && new URL(req.url()).pathname === `/organizations/${ORG}/invitations`) {
        return route.fulfill({
          status: 429,
          contentType: "application/json",
          headers: { "access-control-allow-origin": "*" },
          body: JSON.stringify({ error_code: "invitation.hourly_limit", message: "limit", detail: { limit: 20, window: "hour" } }),
        });
      }
      return backend.handle(route);
    });
    await invite(page, "toomany@example.com");
    await expect(page.getByText("reached its limit of invitations per hour")).toBeVisible();
  });
});

test.describe("Accepting an invitation link", () => {
  test("a fragment token (new emails) is used and removed from the address bar", async ({ page }) => {
    const backend = await open(page, { emailConfigured: true });
    await page.goto("/invitations/inv-9/accept#token=frag-token-123");

    await expect(page.getByRole("heading", { name: "Accept your invitation" })).toBeVisible();
    await expect.poll(() => new URL(page.url()).hash).toBe("");
    await page.getByLabel("Choose a password").fill("a-strong-pass-123");
    await page.getByRole("button", { name: "Accept invitation" }).click();

    await expect(page.getByRole("heading", { name: "Check your email" })).toBeVisible();
    expect(backend.calls.find((c) => c.path === "/invitations/inv-9/accept")!.body).toMatchObject({ token: "frag-token-123" });
  });

  test("an older ?token= link keeps working", async ({ page }) => {
    const backend = await open(page, { emailConfigured: true });
    await page.goto("/invitations/inv-8/accept?token=query-token-456");
    await page.getByLabel("Choose a password").fill("a-strong-pass-123");
    await page.getByRole("button", { name: "Accept invitation" }).click();
    await expect(page.getByRole("heading", { name: "Check your email" })).toBeVisible();
    expect(backend.calls.find((c) => c.path === "/invitations/inv-8/accept")!.body).toMatchObject({ token: "query-token-456" });
  });

  test("a link with no token is reported immediately", async ({ page }) => {
    await open(page, { emailConfigured: true });
    await page.goto("/invitations/inv-7/accept");
    await expect(page.getByRole("heading", { name: "Invalid invitation link" })).toBeVisible();
  });
});
