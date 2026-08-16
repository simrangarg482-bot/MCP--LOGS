import { apiRequest, mockDelay } from "./client";
import { USE_MOCK_DATA } from "./config";
import type { Postmortem, PostmortemUpdatePayload } from "@/types/postmortem";
import type { ApiError } from "@/types/common";

const MOCK_POSTMORTEM: Postmortem = {
  id: "postmortem-1",
  organizationId: "org-1",
  incidentId: "inc-1024",
  status: "draft",
  rootCause: "Null discount configuration object introduced by the promo-code refactor in v2.14.0.",
  actionItems: [
    { description: "Backfill promo_rules for the 3 campaigns missing configuration.", owner: null, status: "open" },
  ],
  generatedBy: "agent:postmortem_agent",
  reviewedBy: null,
  createdAt: new Date().toISOString(),
  updatedAt: new Date().toISOString(),
};

let mockPostmortemStore: Postmortem | null = null;

/**
 * `GET /incidents/{id}/postmortem` -- a Phase 2D backend addition
 * (`core.incidents.service.get_postmortem_by_incident`). Throws a 404
 * `ApiError` (`errorCode: "postmortem.not_found"`) when none exists yet --
 * callers should treat that specific 404 as "offer to generate one," not a
 * genuine error. See `types/common.ts`'s `ApiError.errorCode` for why this
 * is now distinguishable from other error codes at all.
 */
export async function getPostmortemByIncident(incidentId: string): Promise<Postmortem | null> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockPostmortemStore, 300);
  }
  try {
    return await apiRequest<Postmortem>(`/incidents/${incidentId}/postmortem`);
  } catch (err) {
    const apiError = err as ApiError;
    if (apiError?.status === 404) return null;
    throw err;
  }
}

/**
 * `POST /incidents/{id}/postmortem` (`trigger_postmortem_generation`).
 * Real preconditions, not invented: the incident must be `resolved`/
 * `closed` (409 `postmortem.incident_not_resolved` otherwise), and only one
 * postmortem may ever exist per incident (409 `postmortem.already_exists`).
 */
export async function generatePostmortem(incidentId: string): Promise<Postmortem> {
  if (USE_MOCK_DATA) {
    mockPostmortemStore = { ...MOCK_POSTMORTEM, incidentId };
    return mockDelay(mockPostmortemStore, 800);
  }
  return apiRequest<Postmortem>(`/incidents/${incidentId}/postmortem`, { method: "POST" });
}

/** `PATCH /postmortems/{id}` -- only while status is draft/in_review. */
export async function updatePostmortem(
  postmortemId: string,
  patch: PostmortemUpdatePayload,
): Promise<Postmortem> {
  if (USE_MOCK_DATA && mockPostmortemStore) {
    mockPostmortemStore = { ...mockPostmortemStore, ...patch, updatedAt: new Date().toISOString() };
    return mockDelay(mockPostmortemStore, 300);
  }
  return apiRequest<Postmortem>(`/postmortems/${postmortemId}`, { method: "PATCH", body: patch });
}

/**
 * `POST /postmortems/{id}/approve` -- requires `postmortem:approve` (a
 * separate, stronger permission than `postmortem:write`), and only a human
 * identity may call it (never an agent). Sets status to "approved" -- the
 * real terminal state; "published" is a valid enum value but unreachable,
 * no endpoint ever produces it (confirmed by direct backend inspection).
 */
export async function approvePostmortem(postmortemId: string): Promise<Postmortem> {
  if (USE_MOCK_DATA && mockPostmortemStore) {
    mockPostmortemStore = { ...mockPostmortemStore, status: "approved", updatedAt: new Date().toISOString() };
    return mockDelay(mockPostmortemStore, 300);
  }
  return apiRequest<Postmortem>(`/postmortems/${postmortemId}/approve`, { method: "POST" });
}
