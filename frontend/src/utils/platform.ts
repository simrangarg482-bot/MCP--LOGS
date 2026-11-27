import type { ApiError } from "@/types/common";
import type {
  LifecycleAction,
  OrganizationStatus,
  SuspensionNotice,
} from "@/types/platform";

/** The lifecycle actions the database allows from each status (the server enforces this too). */
export function allowedActions(status: OrganizationStatus): LifecycleAction[] {
  switch (status) {
    case "onboarding":
      return ["activate", "suspend"];
    case "active":
      return ["suspend"];
    case "suspended":
      return ["reactivate"];
  }
}

export const REASON_MAX = 1000;
export const MESSAGE_MAX = 1000;
export const NOTE_MAX = 4000;

/** A reason is mandatory for every transition. Returns an error message, or null when acceptable. */
export function validateReason(reason: string): string | null {
  const trimmed = reason.trim();
  if (trimmed.length === 0) return "A reason is required.";
  if (trimmed.length > REASON_MAX) return `The reason must be at most ${REASON_MAX} characters.`;
  return null;
}

export function validateOptionalText(value: string, max: number, label: string): string | null {
  return value.trim().length > max ? `${label} must be at most ${max} characters.` : null;
}

/** `platform.*` errors that mean the operator session (or the whole surface) is no longer usable. */
export function isPlatformSessionFailure(errorCode: string | undefined): boolean {
  return (
    errorCode === "platform.invalid_token" ||
    errorCode === "platform.access_denied" ||
    errorCode === "platform.session_revoked" ||
    errorCode === "platform.unavailable"
  );
}

export function statusTone(status: OrganizationStatus): "neutral" | "success" | "critical" {
  return status === "active" ? "success" : status === "suspended" ? "critical" : "neutral";
}

export function statusLabel(status: OrganizationStatus): string {
  return status === "onboarding" ? "Onboarding" : status === "active" ? "Active" : "Suspended";
}

/**
 * The customer-facing facts the API puts in `detail` of an `auth.organization_suspended` refusal
 * (snake_case, straight off the wire). Anything that is not a string is ignored -- the backend never
 * sends the operator's reason or private note, and this never invents or forwards other fields.
 */
export function suspensionNoticeFromError(error: Pick<ApiError, "detail"> | undefined): SuspensionNotice {
  const detail = (error?.detail ?? {}) as Record<string, unknown>;
  const name = detail["organization_name"];
  const message = detail["customer_message"];
  return {
    organizationName: typeof name === "string" && name.trim() ? name : null,
    customerMessage: typeof message === "string" && message.trim() ? message : null,
  };
}
