import type { UUID } from "./common";

/** `GET /organizations/{id}/permissions` -- one assignable permission
 * (`core.users.role_schemas.PermissionInfo`). Internal permissions such as
 * `system:graph_discovery` are never returned and cannot be granted. */
export interface PermissionInfo {
  code: string;
  label: string;
  description: string;
  group: string;
}

/** `core.users.role_schemas.Role`. `isBuiltin` roles (today only `admin`) are
 * shared by every organization and read-only; custom roles belong to this
 * organization alone and are fully editable. */
export interface Role {
  id: UUID;
  name: string;
  description: string | null;
  isBuiltin: boolean;
  permissions: string[];
  /** Members of *this* organization currently holding the role. */
  memberCount: number;
}

export interface RoleRequest {
  name: string;
  description?: string | null;
  permissions: string[];
}

/** `PUT /organizations/{id}/members/{userId}/roles` response. */
export interface MemberRoles {
  userId: UUID;
  roleIds: UUID[];
  roles: string[];
}

/** `detail.usage` of a `role.in_use` (409) error. */
export interface RoleUsage {
  members: number;
  projectMemberships: number;
  invitations: number;
  accessRules: number;
}
