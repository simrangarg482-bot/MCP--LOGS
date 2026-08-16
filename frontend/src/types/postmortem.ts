import type { UUID } from "./common";

// Matches `app.shared.schemas.common.PostmortemStatus`/`ActionItemStatus`
// exactly. "published" is a real, valid status value but currently
// unreachable in the backend (confirmed by direct code inspection --
// `approve_postmortem` only ever produces "approved", and `PostmortemUpdate.
// status` structurally excludes both "approved" and "published"). Modeled
// here anyway since it IS part of the real enum, but no UI action should
// claim to produce it.
export type PostmortemStatus = "draft" | "in_review" | "approved" | "published";
export type ActionItemStatus = "open" | "in_progress" | "done";

export interface ActionItem {
  description: string;
  owner: string | null;
  status: ActionItemStatus;
}

// Matches `app.core.incidents.schemas.Postmortem` field-for-field.
export interface Postmortem {
  id: UUID;
  organizationId: UUID;
  incidentId: UUID;
  status: PostmortemStatus;
  rootCause: string | null;
  actionItems: ActionItem[];
  generatedBy: string;
  reviewedBy: UUID | null;
  createdAt: string;
  updatedAt: string;
}

// Matches `app.core.incidents.schemas.PostmortemUpdate` -- `status` is
// structurally restricted to draft/in_review only (moving to approved is
// exclusively the separate approve action).
export interface PostmortemUpdatePayload {
  rootCause?: string;
  actionItems?: ActionItem[];
  status?: "draft" | "in_review";
}
