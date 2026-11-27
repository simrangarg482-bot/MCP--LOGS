import type { ApiError } from "@/types/common";
import type { InvitationEmailStatus } from "@/types/tenancy";

export interface EmailStatusView {
  label: string;
  tone: "neutral" | "info" | "success" | "warning" | "critical";
}

/** How the invitation email's delivery state is shown. `sent` only ever means
 * the mail server accepted the message -- never that the person read it. */
export function emailStatusView(status: InvitationEmailStatus): EmailStatusView {
  switch (status) {
    case "queued":
      return { label: "Sending…", tone: "info" };
    case "sent":
      return { label: "Email sent", tone: "success" };
    case "failed":
      return { label: "Email failed", tone: "critical" };
    case "not_configured":
      return { label: "Not emailed", tone: "warning" };
    default:
      return { label: "Not emailed", tone: "neutral" };
  }
}

/** Link an administrator can hand over manually. The token rides in the URL
 * fragment (never sent to servers, logs or Referer); the accept page also still
 * understands the older `?token=` form. */
export function manualInvitationLink(invitationId: string, token: string): string {
  return `${window.location.origin}/invitations/${invitationId}/accept#token=${encodeURIComponent(token)}`;
}

/** Sentence for an error from a create / resend / copy-link call. */
export function invitationErrorMessage(error: unknown): string {
  const apiError = error as Partial<ApiError> | undefined;
  // `detail` is the server's raw (snake_case) object -- see `roleErrorMessage`.
  const raw = (apiError?.detail ?? {}) as Record<string, unknown>;
  const retryAfter = Number(raw.retry_after_seconds ?? raw.retryAfterSeconds ?? 60);
  switch (apiError?.errorCode) {
    case "invitation.hourly_limit":
      return "Your organization has reached its limit of invitations per hour. Please try again later.";
    case "invitation.daily_limit":
      return "Your organization has reached its limit of invitations per day. Please try again tomorrow.";
    case "invitation.resend_cooldown":
      return `Please wait ${retryAfter} seconds before re-sending this invitation.`;
    case "invitation.resend_limit":
      return "This invitation has been re-sent too many times. Copy a new link instead, or revoke it and invite again.";
    case "invitation.already_pending":
      return "An invitation is already pending for this email address.";
    case "invitation.not_pending":
      return "This invitation is no longer pending.";
    case "invitation.not_found":
      return "This invitation no longer exists.";
    case "email.not_configured":
      return "Email delivery isn't configured on this server. Copy a link and send it to the person yourself.";
    case "role.permission_escalation":
      return "You can only invite someone into a role whose permissions you hold yourself.";
    case "role.not_found":
      return "That role no longer exists. Refresh and try again.";
    case "rate_limited.organization":
      return "Too many requests. Please wait a moment and try again.";
    default:
      return apiError?.message || "Something went wrong. Please try again.";
  }
}
