import type { PermissionInfo, Role } from "@/types/roles";

export const mockPermissions: PermissionInfo[] = [
  { code: "incident:read", label: "View incidents", description: "See incidents, their timelines and related details.", group: "Incidents" },
  { code: "incident:write", label: "Create and edit incidents", description: "Report new incidents and update, resolve or annotate existing ones.", group: "Incidents" },
  { code: "postmortem:write", label: "Write postmortems", description: "Draft and edit postmortems for incidents.", group: "Postmortems" },
  { code: "postmortem:approve", label: "Approve postmortems", description: "Review and approve postmortems so they become part of the knowledge base.", group: "Postmortems" },
  { code: "knowledge:review", label: "Review knowledge", description: "Approve or reject knowledge proposed by EKIP before it is published.", group: "Knowledge" },
  { code: "observability:read", label: "View observability", description: "See agent activity, usage and platform health views.", group: "Observability" },
  { code: "audit:read", label: "View audit log", description: "Read the organization's audit trail of who changed what.", group: "Administration" },
  { code: "tenancy:manage", label: "Manage organization", description: "Full administration: users, roles, invitations, access rules, connectors, SSO and projects.", group: "Administration" },
];

const ALL = mockPermissions.map((p) => p.code);

/** Mutable on purpose: the mock `api/roles.ts` mutates it so the Roles page
 * is demonstrable without a backend. Never used when `VITE_USE_MOCK_DATA=false`. */
export const mockRoles: Role[] = [
  { id: "role-admin", name: "admin", description: "Full-access role granted to an organization's first user.", isBuiltin: true, permissions: ALL, memberCount: 2 },
  { id: "role-engineer", name: "engineer", description: "Works incidents: report and update them and write postmortems.", isBuiltin: false, permissions: ["incident:read", "incident:write", "postmortem:write", "observability:read"], memberCount: 2 },
  { id: "role-reviewer", name: "reviewer", description: "Reviews and approves postmortems and proposed knowledge.", isBuiltin: false, permissions: ["incident:read", "observability:read", "postmortem:approve", "knowledge:review"], memberCount: 0 },
  { id: "role-viewer", name: "viewer", description: "Read-only access to incidents and observability.", isBuiltin: false, permissions: ["incident:read", "observability:read"], memberCount: 1 },
];
