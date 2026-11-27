import type { ApiError } from "@/types/common";
import type { ScimOverview, ScimToken } from "@/types/scim";

const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;

/** How long without a successful request before the page warns that synchronization looks stale. */
export const STALE_AFTER_HOURS = 48;
/** How far ahead an expiring token is flagged. */
export const EXPIRING_SOON_DAYS = 14;

export type SyncLevel = "off" | "no_token" | "never" | "ok" | "stale" | "error";

export interface SyncHealth {
  level: SyncLevel;
  message: string;
}

/**
 * Reads the health of the provisioning connection from the real fields the API returns -- nothing is
 * invented. `error` means the most recent request failed AFTER the last success; `stale` means no request
 * succeeded for STALE_AFTER_HOURS.
 */
export function syncHealth(overview: ScimOverview, now: number = Date.now()): SyncHealth {
  if (!overview.platformEnabled) return { level: "off", message: "SCIM is switched off for this deployment." };
  if (!overview.enabled) return { level: "off", message: "Provisioning is turned off for this organization." };
  if (overview.activeTokens === 0) {
    return { level: "no_token", message: "No active token: your identity provider cannot authenticate." };
  }
  const success = overview.lastSuccessAt ? Date.parse(overview.lastSuccessAt) : null;
  const errorAt = overview.lastErrorAt ? Date.parse(overview.lastErrorAt) : null;
  if (success === null) {
    return errorAt !== null
      ? { level: "error", message: "The identity provider has called, but no request has succeeded yet." }
      : { level: "never", message: "No request has been received yet." };
  }
  if (errorAt !== null && errorAt > success) {
    return { level: "error", message: "The most recent request failed after the last successful one." };
  }
  if (now - success > STALE_AFTER_HOURS * HOUR) {
    return { level: "stale", message: `No successful sync for over ${STALE_AFTER_HOURS} hours.` };
  }
  return { level: "ok", message: "Synchronization is healthy." };
}

/** Warnings about the credentials themselves: none active, or one expiring soon. */
export function tokenWarnings(tokens: ScimToken[], now: number = Date.now()): string[] {
  const active = tokens.filter((t) => t.status === "active");
  const warnings: string[] = [];
  const expiring = active.filter((t) => Date.parse(t.expiresAt) - now < EXPIRING_SOON_DAYS * DAY);
  for (const t of expiring) {
    const days = Math.max(0, Math.ceil((Date.parse(t.expiresAt) - now) / DAY));
    warnings.push(`The token "${t.name}" expires in ${days} day${days === 1 ? "" : "s"}. Rotate it before then.`);
  }
  if (tokens.length > 0 && active.length === 0) {
    warnings.push("Every token has expired or been revoked. Create a new one and give it to your identity provider.");
  }
  return warnings;
}

const ERROR_TEXT: Record<string, string> = {
  "scim.too_many_tokens": "This organization already has the maximum number of active tokens. Revoke one first.",
  "scim.invalid_token_ttl": "That expiry is outside the allowed range.",
  "scim.token_not_active": "Only an active token can be rotated.",
  "scim.already_linked": "That identity is already linked to a member.",
  "scim.user_already_linked": "That member is already linked to another identity.",
  "scim.not_a_member": "That person is not a member of this organization.",
  "scim.pending_decided": "Someone has already decided this request.",
  "scim.pending_not_found": "That request no longer exists.",
  "scim.group_not_found": "That group no longer exists.",
  "role.not_found": "One of those roles no longer exists.",
  "role.permission_escalation": "You cannot map a role that has permissions you do not hold yourself.",
  "member.role_group_managed":
    "This role is granted by an identity-provider group. Change the member's group membership or the group's role mapping instead.",
  "org.last_admin": "That would leave the organization without an administrator. Give another user admin access first.",
  "member.cannot_deactivate_self": "You cannot deactivate yourself.",
  "member.already_deactivated": "This member is already deactivated.",
  "member.not_deactivated": "This member is not deactivated.",
  "member.not_found": "That member was not found.",
  "users.cross_organization_denied": "You can only manage your own organization.",
};

export function scimErrorMessage(error: unknown, fallback = "Something went wrong. Try again."): string {
  const e = error as Partial<ApiError> | undefined;
  if (e?.errorCode && ERROR_TEXT[e.errorCode]) return ERROR_TEXT[e.errorCode];
  if (e?.status === 403) return "You need the tenancy:manage permission to do this.";
  return typeof e?.message === "string" && e.message ? e.message : fallback;
}

export function tokenStatusTone(status: ScimToken["status"]): "success" | "warning" | "critical" {
  return status === "active" ? "success" : status === "expired" ? "warning" : "critical";
}

// --- audit log: readable descriptions and filter groups ------------------------------------------------

export const AUDIT_ACTION_GROUPS: Array<{ value: string; label: string }> = [
  { value: "", label: "All events" },
  { value: "member.", label: "Member access" },
  { value: "scim.", label: "SCIM provisioning" },
];

const AUDIT_TEXT: Record<string, string> = {
  "member.deactivate": "Member access deactivated",
  "member.reactivate": "Member access reactivated",
  "scim.user_created": "Identity provider announced a user",
  "scim.user_deleted": "Identity provider deleted a user",
  "scim.identity_linked": "Identity linked to a member",
  "scim.deactivation_held": "Deactivation held for approval",
  "scim.deactivation_approved": "Held deactivation approved",
  "scim.deactivation_rejected": "Held deactivation rejected",
  "scim.deactivation_refused": "Deactivation refused",
  "scim.reactivation_ignored": "Re-enable ignored",
  "scim.reactivation_deferred": "Re-enable deferred",
  "scim.token_created": "SCIM token created",
  "scim.token_rotated": "SCIM token rotated",
  "scim.token_revoked": "SCIM token revoked",
  "scim.config_updated": "Provisioning settings changed",
  "scim.group_created": "Identity provider created a group",
  "scim.group_renamed": "Identity provider renamed a group",
  "scim.group_deleted": "Identity provider deleted a group",
  "scim.group_members_changed": "Group membership changed",
  "scim.group_roles_updated": "Group role mapping changed",
  "scim.role_granted": "Role granted through a group",
  "scim.role_revoked": "Role withdrawn through a group",
  "scim.role_revoke_refused": "Role withdrawal refused",
  "scim.role_revoke_held": "Role withdrawal held",
};

/** A sentence for an audit action; unknown actions are shown as recorded. */
export function describeAuditAction(action: string, metadata?: Record<string, unknown> | null): string {
  const base = AUDIT_TEXT[action];
  if (!base) return action;
  const source = metadata?.["source"];
  if ((action === "member.deactivate" || action === "member.reactivate") && source === "scim") {
    return `${base} by the identity provider`;
  }
  if ((action === "scim.deactivation_refused" || action === "scim.role_revoke_refused") && metadata?.["reason"] === "last_admin") {
    return `${base} (last administrator)`;
  }
  return base;
}

/** The reason recorded with an event, when there is one. Never shows anything the API did not send. */
export function auditReason(metadata?: Record<string, unknown> | null): string | null {
  const reason = metadata?.["reason"];
  return typeof reason === "string" && reason ? reason : null;
}
