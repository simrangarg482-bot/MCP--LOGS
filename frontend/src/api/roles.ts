import { apiRequest, mockDelay } from "./client";
import { USE_MOCK_DATA } from "./config";
import type { MemberRoles, PermissionInfo, Role, RoleRequest } from "@/types/roles";
import { mockPermissions, mockRoles } from "@/mocks/data/roles";

/** `GET /organizations/{id}/permissions` -- the assignable catalog (readable
 * by any member of the organization). */
export async function listPermissions(organizationId: string): Promise<PermissionInfo[]> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockPermissions, 150);
  }
  return apiRequest<PermissionInfo[]>(`/organizations/${organizationId}/permissions`);
}

/** `GET /organizations/{id}/roles` (`tenancy:manage`) -- built-in roles plus
 * this organization's own, each with its permission codes and member count. */
export async function listRoles(organizationId: string): Promise<Role[]> {
  if (USE_MOCK_DATA) {
    return mockDelay([...mockRoles], 200);
  }
  return apiRequest<Role[]>(`/organizations/${organizationId}/roles`);
}

/** `POST /organizations/{id}/roles` (`tenancy:manage`). */
export async function createRole(organizationId: string, data: RoleRequest): Promise<Role> {
  if (USE_MOCK_DATA) {
    const role: Role = {
      id: `role-${Date.now()}`,
      name: data.name,
      description: data.description ?? null,
      isBuiltin: false,
      permissions: data.permissions,
      memberCount: 0,
    };
    mockRoles.push(role);
    return mockDelay(role, 250);
  }
  return apiRequest<Role>(`/organizations/${organizationId}/roles`, { method: "POST", body: data });
}

/** `PATCH /organizations/{id}/roles/{roleId}` -- only the fields present are
 * changed. Built-in roles are refused (`role.builtin_immutable`). */
export async function updateRole(
  organizationId: string,
  roleId: string,
  data: Partial<RoleRequest>,
): Promise<Role> {
  if (USE_MOCK_DATA) {
    const role = mockRoles.find((r) => r.id === roleId);
    if (!role || role.isBuiltin) throw { status: 403, message: "Built-in roles cannot be modified.", errorCode: "role.builtin_immutable" };
    Object.assign(role, data);
    return mockDelay({ ...role }, 250);
  }
  return apiRequest<Role>(`/organizations/${organizationId}/roles/${roleId}`, {
    method: "PATCH",
    body: data,
  });
}

/** `DELETE /organizations/{id}/roles/{roleId}` (204). Refused while the role
 * is still assigned, invited or used by an access rule (`role.in_use`). */
export async function deleteRole(organizationId: string, roleId: string): Promise<void> {
  if (USE_MOCK_DATA) {
    const index = mockRoles.findIndex((r) => r.id === roleId);
    if (index >= 0 && !mockRoles[index].isBuiltin && mockRoles[index].memberCount === 0) mockRoles.splice(index, 1);
    return mockDelay(undefined, 200);
  }
  return apiRequest<void>(`/organizations/${organizationId}/roles/${roleId}`, { method: "DELETE" });
}

/** `PUT /organizations/{id}/members/{userId}/roles` -- replaces the member's
 * whole role set (at least one role). */
export async function setMemberRoles(
  organizationId: string,
  userId: string,
  roleIds: string[],
): Promise<MemberRoles> {
  if (USE_MOCK_DATA) {
    return mockDelay(
      { userId, roleIds, roles: mockRoles.filter((r) => roleIds.includes(r.id)).map((r) => r.name) },
      250,
    );
  }
  return apiRequest<MemberRoles>(`/organizations/${organizationId}/members/${userId}/roles`, {
    method: "PUT",
    body: { roleIds },
  });
}
