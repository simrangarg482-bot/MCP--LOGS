import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * UI tests for Settings -> Roles & Permissions, the invite dropdown, member
 * role editing and "My Roles".
 *
 * Self-contained: every backend call is answered by a small stateful fake
 * installed with `page.route`, so this needs no database, no Redis and no
 * running API -- only the Vite dev server, built with
 * `VITE_USE_MOCK_DATA=false VITE_API_BASE_URL=http://127.0.0.1:8010`.
 * It checks what the UI does with the API contract; the rules themselves
 * (RLS isolation, last administrator, escalation, ...) are enforced and tested
 * server-side in `tests/core/users/test_role_management_postgres.py`. The fake
 * mimics only the error responses the UI must handle.
 */

const API = process.env.E2E_API_BASE_URL ?? "http://127.0.0.1:8010";
const ORG = "11111111-1111-4111-8111-111111111111";
const ME = "22222222-2222-4222-8222-222222222222";

const CATALOG = [
  ["incident:read", "View incidents", "Incidents"],
  ["incident:write", "Create and edit incidents", "Incidents"],
  ["postmortem:write", "Write postmortems", "Postmortems"],
  ["postmortem:approve", "Approve postmortems", "Postmortems"],
  ["knowledge:review", "Review knowledge", "Knowledge"],
  ["observability:read", "View observability", "Observability"],
  ["audit:read", "View audit log", "Administration"],
  ["tenancy:manage", "Manage organization", "Administration"],
].map(([code, label, group]) => ({ code, label, description: `${label}.`, group }));

interface FakeRole { id: string; name: string; description: string | null; is_builtin: boolean; permissions: string[]; member_count: number }
interface FakeMember { id: string; email: string; display_name: string; is_active: boolean; role_ids: string[] }

function makeBackend(myPermissions: string[]) {
  const roles: FakeRole[] = [
    { id: "r-admin", name: "admin", description: "Full access.", is_builtin: true, permissions: CATALOG.map((p) => p.code), member_count: 1 },
    { id: "r-viewer", name: "viewer", description: "Read-only.", is_builtin: false, permissions: ["incident:read", "observability:read"], member_count: 0 },
    { id: "r-eng", name: "engineer", description: "Works incidents.", is_builtin: false, permissions: ["incident:read", "incident:write", "postmortem:write", "observability:read"], member_count: 1 },
  ];
  const members: FakeMember[] = [
    { id: ME, email: "me@example.com", display_name: "Me Admin", is_active: true, role_ids: ["r-admin"] },
    { id: "u-bob", email: "bob@example.com", display_name: "Bob Engineer", is_active: true, role_ids: ["r-eng"] },
  ];
  const calls: Array<{ method: string; path: string; body: unknown }> = [];
  let editedRoles = false;
  const names = (ids: string[]) => ids.map((id) => roles.find((r) => r.id === id)!.name);

  const json = (route: Route, status: number, body: unknown) =>
    route.fulfill({ status, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(body) });
  const err = (route: Route, status: number, code: string, message: string, detail: unknown = {}) =>
    json(route, status, { error_code: code, message, detail });

  async function handle(route: Route) {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname;
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
      const mine = members.find((m) => m.id === ME)!;
      const perms = new Set<string>(myPermissions);
      // roles edited during a test change what /auth/me reports (like the real backend)
      if (editedRoles) {
        perms.clear();
        names(mine.role_ids).forEach((n) => roles.find((r) => r.name === n)!.permissions.forEach((p) => perms.add(p)));
      }
      return json(route, 200, { id: ME, email: "me@example.com", display_name: "Me Admin", is_active: true, roles: names(mine.role_ids), permissions: [...perms] });
    }
    if (path === "/auth/organizations") return json(route, 200, { organizations: [{ id: ORG, name: "Acme", slug: "acme" }] });
    if (path === "/organizations") return json(route, 200, [{ id: ORG, name: "Acme", slug: "acme" }]);
    if (path === `/organizations/${ORG}/projects`) return json(route, 200, []);
    if (path === `/organizations/${ORG}/permissions`) return json(route, 200, CATALOG);
    if (path === `/organizations/${ORG}/invitations` && method === "GET") return json(route, 200, []);
    if (path === `/organizations/${ORG}/invitations` && method === "POST") {
      return json(route, 201, { id: "inv-1", organization_id: ORG, email: body.email, status: "pending", expires_at: "2099-01-01T00:00:00Z", email_status: "not_configured", token: "tok" });
    }
    if (path === `/organizations/${ORG}/members`) {
      return json(route, 200, members.map((m) => ({ id: m.id, email: m.email, display_name: m.display_name, is_active: m.is_active, roles: names(m.role_ids), role_ids: m.role_ids, created_at: "2026-01-01T00:00:00Z" })));
    }
    if (path === `/organizations/${ORG}/roles` && method === "GET") {
      if (!myPermissions.includes("tenancy:manage")) return err(route, 403, "permission_denied", "no");
      roles.forEach((r) => (r.member_count = members.filter((m) => m.role_ids.includes(r.id)).length));
      return json(route, 200, roles);
    }
    if (path === `/organizations/${ORG}/roles` && method === "POST") {
      if (roles.some((r) => r.name.toLowerCase() === String(body.name).toLowerCase())) {
        return err(route, 409, "role.name_taken", "A role with this name already exists.");
      }
      const role: FakeRole = { id: `r-${roles.length}`, name: body.name, description: body.description ?? null, is_builtin: false, permissions: body.permissions, member_count: 0 };
      roles.push(role);
      return json(route, 201, role);
    }
    const roleMatch = path.match(new RegExp(`^/organizations/${ORG}/roles/([^/]+)$`));
    if (roleMatch) {
      const role = roles.find((r) => r.id === roleMatch[1]);
      if (!role) return err(route, 404, "role.not_found", "Role not found.");
      if (role.is_builtin) return err(route, 403, "role.builtin_immutable", "Built-in roles cannot be modified or deleted.");
      if (method === "PATCH") {
        const holders = members.filter((m) => m.role_ids.includes(role.id));
        const otherAdmins = members.filter(
          (m) => !holders.includes(m) && m.role_ids.some((id) => roles.find((r) => r.id === id)!.permissions.includes("tenancy:manage")),
        );
        if (holders.length && !otherAdmins.length && body.permissions && role.permissions.includes("tenancy:manage") && !body.permissions.includes("tenancy:manage")) {
          return err(route, 409, "org.last_admin", "This change would leave the organization without an administrator.");
        }
        Object.assign(role, body);
        editedRoles = true;
        return json(route, 200, role);
      }
      if (method === "DELETE") {
        const used = members.filter((m) => m.role_ids.includes(role.id)).length;
        if (used) return err(route, 409, "role.in_use", "in use", { usage: { members: used, project_memberships: 0, invitations: 0, access_rules: 0 } });
        roles.splice(roles.indexOf(role), 1);
        return route.fulfill({ status: 204, headers: { "access-control-allow-origin": "*" } });
      }
    }
    const memberMatch = path.match(new RegExp(`^/organizations/${ORG}/members/([^/]+)/roles$`));
    if (memberMatch && method === "PUT") {
      const member = members.find((m) => m.id === memberMatch[1])!;
      const adminIds = members.filter((m) => m.role_ids.some((id) => roles.find((r) => r.id === id)!.permissions.includes("tenancy:manage")));
      const stillAdmin = (body.role_ids as string[]).some((id) => roles.find((r) => r.id === id)!.permissions.includes("tenancy:manage"));
      if (adminIds.length === 1 && adminIds[0].id === member.id && !stillAdmin) {
        return err(route, 409, "org.last_admin", "This change would leave the organization without an administrator.");
      }
      member.role_ids = body.role_ids;
      editedRoles = true;
      return json(route, 200, { user_id: member.id, role_ids: member.role_ids, roles: names(member.role_ids) });
    }
    return json(route, 200, []);
  }

  return { handle, calls, roles, members };
}

async function openAs(page: Page, myPermissions: string[]) {
  const backend = makeBackend(myPermissions);
  await page.addInitScript(() => localStorage.setItem("ekip.refresh_token", "rt1"));
  await page.route(`${API}/**`, backend.handle);
  return backend;
}

test.describe("Roles & Permissions UI", () => {
  test("lists built-in and custom roles with permission counts", async ({ page }) => {
    await openAs(page, CATALOG.map((p) => p.code));
    await page.goto("/settings/roles");

    await expect(page.getByRole("heading", { name: "Roles & permissions" })).toBeVisible();
    const admin = page.getByRole("row", { name: /admin/ });
    await expect(admin.getByText("Built-in")).toBeVisible();
    await expect(admin.getByText("8 of 8")).toBeVisible();
    await expect(admin.getByRole("button", { name: "View" })).toBeVisible();
    await expect(admin.getByRole("button", { name: "Delete" })).toHaveCount(0);
    const viewer = page.getByRole("row", { name: /viewer/ });
    await expect(viewer.getByText("Custom")).toBeVisible();
    await expect(viewer.getByText("2 of 8")).toBeVisible();
  });

  test("a built-in role opens read-only", async ({ page }) => {
    await openAs(page, CATALOG.map((p) => p.code));
    await page.goto("/settings/roles");
    await page.getByRole("row", { name: /admin/ }).getByRole("button", { name: "View" }).click();

    const dialog = page.getByRole("dialog");
    await expect(dialog.getByText("Read-only.")).toBeVisible();
    await expect(dialog.getByLabel("Role name")).toHaveAttribute("readonly", "");
    for (const box of await dialog.getByRole("checkbox").all()) await expect(box).toBeDisabled();
    await expect(dialog.getByRole("button", { name: "Save changes" })).toHaveCount(0);
  });

  test("creates a custom role with grouped permission checkboxes", async ({ page }) => {
    const backend = await openAs(page, CATALOG.map((p) => p.code));
    await page.goto("/settings/roles");
    await page.getByRole("button", { name: "New role" }).click();

    const dialog = page.getByRole("dialog");
    await expect(dialog.getByText("Incidents", { exact: true })).toBeVisible();
    await expect(dialog.getByText("Administration", { exact: true })).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Create role" })).toBeDisabled();
    await dialog.getByLabel("Role name").fill("Incident Commander");
    await dialog.getByLabel("Role description").fill("Runs incidents");
    await dialog.getByLabel("View incidents").check();
    await dialog.getByLabel("Create and edit incidents").check();
    await dialog.getByRole("button", { name: "Create role" }).click();

    await expect(page.getByRole("row", { name: /Incident Commander/ })).toBeVisible();
    const post = backend.calls.find((c) => c.method === "POST" && c.path.endsWith("/roles"))!;
    expect(post.body).toMatchObject({ name: "Incident Commander", permissions: ["incident:read", "incident:write"] });
  });

  test("a duplicate role name shows the server's reason inside the dialog", async ({ page }) => {
    await openAs(page, CATALOG.map((p) => p.code));
    await page.goto("/settings/roles");
    await page.getByRole("button", { name: "New role" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Role name").fill("Engineer");
    await dialog.getByRole("button", { name: "Create role" }).click();
    await expect(dialog.getByRole("alert")).toContainText("A role with this name already exists");
  });

  test("edits a role's permissions and warns before removing admin power", async ({ page }) => {
    const backend = await openAs(page, CATALOG.map((p) => p.code));
    backend.roles.push({ id: "r-ops", name: "ops", description: null, is_builtin: false, permissions: ["tenancy:manage", "incident:read"], member_count: 0 });
    await page.goto("/settings/roles");
    await page.getByRole("row", { name: /ops/ }).getByRole("button", { name: "Edit" }).click();
    const dialog = page.getByRole("dialog");

    await dialog.getByLabel("Manage organization").uncheck();
    await expect(dialog.getByText("will lose administrator access")).toBeVisible();
    await dialog.getByLabel("View audit log").check();
    await dialog.getByRole("button", { name: "Save changes" }).click();

    await expect(dialog).toBeHidden();
    const patch = backend.calls.find((c) => c.method === "PATCH")!;
    expect(patch.body).toMatchObject({ permissions: ["incident:read", "audit:read"] });
  });

  test("deleting a role in use is blocked, an unused one is deleted after confirmation", async ({ page }) => {
    const backend = await openAs(page, CATALOG.map((p) => p.code));
    await page.goto("/settings/roles");

    // the engineer role has a member: the button is disabled with an explanation
    const engineer = page.getByRole("row", { name: /engineer/ });
    await expect(engineer.getByRole("button", { name: "Delete" })).toBeDisabled();

    await page.getByRole("row", { name: /viewer/ }).getByRole("button", { name: "Delete" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Delete role" }).click();
    await expect(page.getByRole("row", { name: /viewer/ })).toHaveCount(0);
    expect(backend.calls.some((c) => c.method === "DELETE")).toBeTruthy();
  });

  test("a non-administrator sees an explanation instead of the roles table", async ({ page }) => {
    await openAs(page, ["incident:read"]);
    await page.goto("/settings/roles");
    await expect(page.getByText("Roles & permissions are managed by administrators")).toBeVisible();
    await expect(page.getByRole("link", { name: "Roles & Permissions" })).toHaveCount(0);
    await expect(page.getByRole("link", { name: "My Roles" })).toBeVisible();
  });

  test("a permission the editor does not hold cannot be granted", async ({ page }) => {
    // administrator-by-permission, but without audit:read and incident:write
    await openAs(page, ["tenancy:manage", "incident:read"]);
    await page.goto("/settings/roles");
    await page.getByRole("button", { name: "New role" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByLabel("View incidents")).toBeEnabled();
    await expect(dialog.getByLabel("View audit log")).toBeDisabled();
    await expect(dialog.getByLabel("Create and edit incidents")).toBeDisabled();
  });
});

test.describe("Invitations and member roles UI", () => {
  test("the invite form offers a role dropdown and previews the role's permissions", async ({ page }) => {
    const backend = await openAs(page, CATALOG.map((p) => p.code));
    await page.goto("/settings/users");
    await page.getByRole("button", { name: "Invite user" }).click();
    const dialog = page.getByRole("dialog");

    const select = dialog.getByLabel("Role");
    await expect(select).toBeVisible();
    // defaults to the least privileged role, never silently to admin
    await expect(select.locator("option:checked")).toHaveText("viewer");
    await expect(dialog.getByTestId("role-permission-summary")).toContainText("View incidents");
    await expect(dialog.getByTestId("role-permission-summary")).not.toContainText("Manage organization");

    await select.selectOption({ label: "engineer" });
    await expect(dialog.getByTestId("role-permission-summary")).toContainText("Write postmortems");
    await dialog.getByLabel("Email").fill("new@example.com");
    await dialog.getByRole("button", { name: "Send invitation" }).click();

    await expect(dialog.getByTestId("invitation-link-panel")).toBeVisible();
    const post = backend.calls.find((c) => c.method === "POST" && c.path.endsWith("/invitations"))!;
    expect(post.body).toMatchObject({ email: "new@example.com", grants_role: "engineer" });
  });

  test("roles the inviter cannot fully grant are disabled in the dropdown", async ({ page }) => {
    await openAs(page, ["tenancy:manage", "incident:read", "observability:read"]);
    await page.goto("/settings/users");
    await page.getByRole("button", { name: "Invite user" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByRole("option", { name: /^admin/ })).toHaveAttribute("disabled", "");
    await expect(dialog.getByRole("option", { name: /^engineer/ })).toHaveAttribute("disabled", "");
    await expect(dialog.getByRole("option", { name: /^viewer/ })).not.toHaveAttribute("disabled", "");
  });

  test("members show all their roles and an administrator can change them", async ({ page }) => {
    const backend = await openAs(page, CATALOG.map((p) => p.code));
    await page.goto("/settings/users");

    const bob = page.getByRole("row", { name: /Bob Engineer/ });
    await expect(bob.getByText("engineer", { exact: true })).toBeVisible();
    await bob.getByRole("button", { name: "Manage roles" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByLabel("engineer")).toBeChecked();
    await expect(dialog.getByRole("button", { name: "Save roles" })).toBeDisabled(); // unchanged
    await dialog.getByLabel("viewer").check();
    await dialog.getByLabel("engineer").uncheck();
    await dialog.getByRole("button", { name: "Save roles" }).click();

    await expect(dialog).toBeHidden();
    await expect(page.getByRole("row", { name: /Bob Engineer/ }).getByText("viewer", { exact: true })).toBeVisible();
    const put = backend.calls.find((c) => c.method === "PUT")!;
    expect(put.body).toEqual({ role_ids: ["r-viewer"] });
  });

  test("a member can hold several roles and cannot be left with none", async ({ page }) => {
    await openAs(page, CATALOG.map((p) => p.code));
    await page.goto("/settings/users");
    await page.getByRole("row", { name: /Bob Engineer/ }).getByRole("button", { name: "Manage roles" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("viewer").check();
    await dialog.getByRole("button", { name: "Save roles" }).click();
    await expect(dialog).toBeHidden();
    const bob = page.getByRole("row", { name: /Bob Engineer/ });
    await expect(bob.getByText("engineer", { exact: true })).toBeVisible();
    await expect(bob.getByText("viewer", { exact: true })).toBeVisible();

    await bob.getByRole("button", { name: "Manage roles" }).click();
    await page.getByRole("dialog").getByLabel("engineer").uncheck();
    await page.getByRole("dialog").getByLabel("viewer").uncheck();
    await expect(page.getByRole("dialog").getByRole("button", { name: "Save roles" })).toBeDisabled();
  });

  test("demoting the last administrator is refused with a clear message", async ({ page }) => {
    await openAs(page, CATALOG.map((p) => p.code));
    await page.goto("/settings/users");
    await page.getByRole("row", { name: /Me Admin/ }).getByRole("button", { name: "Manage roles" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("viewer").check();
    await dialog.getByLabel("admin", { exact: true }).uncheck();
    await dialog.getByRole("button", { name: "Save roles" }).click();
    await expect(dialog.getByRole("alert")).toContainText("without an administrator");
    await expect(dialog).toBeVisible();
  });
});

test.describe("My Roles", () => {
  test("shows the user's roles and which permissions are granted", async ({ page }) => {
    await openAs(page, ["incident:read", "observability:read"]);
    await page.goto("/settings/my-access");

    await expect(page.getByTestId("my-roles")).toContainText("admin");
    const read = page.locator('[data-permission="incident:read"]');
    await expect(read).toHaveAttribute("data-granted", "true");
    await expect(page.locator('[data-permission="tenancy:manage"]')).toHaveAttribute("data-granted", "false");
    await expect(page.getByText("View audit log", { exact: true })).toBeVisible();
  });
});
