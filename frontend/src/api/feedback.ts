import { apiRequest, mockDelay } from "./client";
import { USE_MOCK_DATA } from "./config";
import type { AnswerFeedback, FeedbackSubmitPayload } from "@/types/ask";
import type { UUID } from "@/types/common";

// ---- mock layer -----------------------------------------------------------
// In-memory, per page load -- enough to demo the controls without a backend.
// An execution id of `MOCK_UNRATEABLE_EXECUTION_ID` always fails with a 404,
// the response the real API gives for an execution you may not rate; Ask's
// mock returns that id for a question containing "unrateable" so the error
// state can be exercised deterministically.
export const MOCK_UNRATEABLE_EXECUTION_ID = "00000000-0000-0000-0000-00000000dead";

const mockStore = new Map<UUID, AnswerFeedback>();

/**
 * `GET /ask/executions/{id}/feedback` -- the caller's own rating, or `null`.
 * Lets the controls show prior feedback after a reload.
 */
export async function getAnswerFeedback(executionId: UUID): Promise<AnswerFeedback | null> {
  if (USE_MOCK_DATA) {
    return mockDelay(mockStore.get(executionId) ?? null, 150);
  }
  return apiRequest<AnswerFeedback | null>(`/ask/executions/${executionId}/feedback`);
}

/**
 * `PUT /ask/executions/{id}/feedback` -- create or replace the caller's one
 * rating of this execution. Idempotent: the same body twice changes nothing.
 * The organization and user are never sent; the server derives both from the
 * session.
 */
export async function submitAnswerFeedback(
  executionId: UUID,
  payload: FeedbackSubmitPayload,
): Promise<AnswerFeedback> {
  if (USE_MOCK_DATA) {
    if (executionId === MOCK_UNRATEABLE_EXECUTION_ID) {
      await mockDelay(null, 300);
      throw { status: 404, message: "Execution not found.", errorCode: "feedback.execution_not_found" };
    }
    const now = new Date().toISOString();
    const existing = mockStore.get(executionId);
    const saved: AnswerFeedback = {
      id: existing?.id ?? `mock-feedback-${mockStore.size + 1}`,
      executionId,
      rating: payload.rating,
      reasonCode: payload.rating === "not_helpful" ? (payload.reasonCode ?? null) : null,
      comment: payload.comment ?? null,
      createdAt: existing?.createdAt ?? now,
      updatedAt: now,
    };
    mockStore.set(executionId, saved);
    return mockDelay(saved, 400);
  }
  return apiRequest<AnswerFeedback>(`/ask/executions/${executionId}/feedback`, {
    method: "PUT",
    body: payload,
  });
}
