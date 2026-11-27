import type { ApiError } from "@/types/common";
import type { PermissionInfo, Role } from "@/types/roles";

/** Permission code whose holders are the organization's administrators. */
export const ADMIN_PERMISSION = "tenancy:manage";

/** Can the signed-in user hand out `role`? The backend refuses a role carrying
 * any permission the caller lacks (`role.permission_escalation`); this mirrors
 * that rule only to disable the option up front -- it is UX, not enforcement. */
export function canGrantRole(role: Pick<Role, "permissions">, myPermissions: readonly string[]): boolean {
  const mine = new Set(myPermissions);
  return role.permissions.every((code) => mine.has(code));
}

/** Union of the permission codes of the roles named by `roleIds`. */
export function unionPermissions(roles: readonly Role[], roleIds: readonly string[]): string[] {
  const selected = new Set(roleIds);
  const codes = new Set<string>();
  for (const role of roles) {
    if (selected.has(role.id)) role.permissions.forEach((code) => codes.add(code));
  }
  return [...codes];
}

/** Group the catalog by its `group`, keeping the server's order. */
export function groupPermissions(catalog: readonly PermissionInfo[]): Array<[string, PermissionInfo[]]> {
  const groups = new Map<string, PermissionInfo[]>();
  for (const permission of catalog) {
    const list = groups.get(permission.group) ?? [];
    list.push(permission);
    groups.set(permission.group, list);
  }
  return [...groups.entries()];
}

export function permissionLabel(catalog: readonly PermissionInfo[], code: string): string {
  return catalog.find((p) => p.code === code)?.label ?? code;
}

/** Error `detail` arrives exactly as the server sent it (snake_case) -- the API
 * client only converts successful response bodies -- so accept both spellings. */
function pick(source: Record<string, unknown>, snake: string, camel: string): number {
  return Number(source[snake] ?? source[camel] ?? 0);
}

function describeUsage(usage: Record<string, unknown> | undefined): string {
  if (!usage) return "";
  const members = pick(usage, "members", "members");
  const projects = pick(usage, "project_memberships", "projectMemberships");
  const invitations = pick(usage, "invitations", "invitations");
  const rules = pick(usage, "access_rules", "accessRules");
  const parts: string[] = [];
  if (members) parts.push(`${members} member${members === 1 ? "" : "s"}`);
  if (projects) parts.push(`${projects} project membership${projects === 1 ? "" : "s"}`);
  if (invitations) parts.push(`${invitations} invitation${invitations === 1 ? "" : "s"}`);
  if (rules) parts.push(`${rules} access rule${rules === 1 ? "" : "s"}`);
  return parts.length ? ` It is still used by ${parts.join(", ")}.` : "";
}

/** Turn an API error from a role-management call into a sentence a person can
 * act on. Falls back to the server's own message. */
export function roleErrorMessage(error: unknown): string {
  const apiError = error as Partial<ApiError> | undefined;
  switch (apiError?.errorCode) {
    case "role.name_taken":
      return "A role with this name already exists. Choose a different name.";
    case "role.builtin_immutable":
      return "Built-in roles cannot be modified or deleted.";
    case "role.permission_escalation":
      return "You can only grant permissions that you hold yourself.";
    case "role.unknown_permission":
      return "One of the selected permissions is not available.";
    case "role.in_use": {
      const detail = apiError.detail as { usage?: Record<string, unknown> } | undefined;
      return `Remove this role from everything that uses it before deleting it.${describeUsage(detail?.usage)}`;
    }
    case "org.last_admin":
      return "That would leave the organization without an administrator. Give another user admin access first.";
    case "member.last_role":
      return "A member must keep at least one role.";
    case "member.not_found":
      return "That member no longer belongs to this organization.";
    case "role.not_found":
      return "That role no longer exists. Refresh and try again.";
    case "permission_denied":
      return "You don't have permission to manage roles.";
    default:
      return apiError?.message || "Something went wrong. Please try again.";
  }
}
