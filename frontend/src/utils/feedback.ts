import type { ApiError } from "@/types/common";
import type {
  AskResponse,
  FeedbackReasonCode,
  FeedbackRating,
  FeedbackSubmitPayload,
} from "@/types/ask";

// Pure helpers behind `components/domain/AnswerFeedback` -- no React, no
// network -- so the rules that matter (when a click may submit, what is sent,
// what the user is told on failure) are unit-testable without a browser.

/** Mirrors the backend's `MAX_COMMENT_LENGTH` (`agents.feedback.schemas`). */
export const MAX_FEEDBACK_COMMENT_LENGTH = 2000;

export const FEEDBACK_REASON_OPTIONS: ReadonlyArray<{ value: FeedbackReasonCode; label: string }> = [
  { value: "incorrect", label: "Incorrect" },
  { value: "incomplete", label: "Incomplete" },
  { value: "outdated", label: "Out of date" },
  { value: "irrelevant_sources", label: "Wrong sources" },
  { value: "no_answer_needed_more", label: "Should have found an answer" },
  { value: "other", label: "Other" },
];

/** Only a response that carries an execution id can be rated. */
export function isFeedbackEligible(response: Pick<AskResponse, "executionId"> | undefined): boolean {
  return !!response?.executionId;
}

/**
 * Whether clicking `clicked` should send a request. A click is ignored while
 * a request is in flight (double-click / impatient re-click) and when it
 * would just re-send the rating already saved; clicking the OTHER rating is
 * how a person changes their mind.
 */
export function shouldSubmitRating(
  current: FeedbackRating | null,
  clicked: FeedbackRating,
  isPending: boolean,
): boolean {
  return !isPending && current !== clicked;
}

/** Null when valid, otherwise a message for the user. */
export function validateFeedbackComment(comment: string): string | null {
  if (comment.length > MAX_FEEDBACK_COMMENT_LENGTH) {
    return `Please keep this under ${MAX_FEEDBACK_COMMENT_LENGTH} characters.`;
  }
  return null;
}

/**
 * The request body. A reason only qualifies a negative rating, and a blank
 * comment is omitted rather than sent as an empty string.
 */
export function buildFeedbackPayload(
  rating: FeedbackRating,
  reasonCode: FeedbackReasonCode | null,
  comment: string,
): FeedbackSubmitPayload {
  const trimmed = comment.trim();
  return {
    rating,
    reasonCode: rating === "not_helpful" ? reasonCode : null,
    comment: trimmed.length > 0 ? trimmed : null,
  };
}

/** What a person is told when saving feedback fails. Never echoes server internals. */
export function feedbackErrorMessage(error: unknown): string {
  const status = (error as Partial<ApiError> | null)?.status;
  switch (status) {
    case 404:
      return "This answer can no longer be rated.";
    case 409:
      return "This answer can't be rated because it didn't complete.";
    case 429:
      return "You're sending feedback too quickly. Please wait a moment and try again.";
    case 422:
      return "That feedback couldn't be saved. Please check what you entered.";
    default:
      return "Couldn't save your feedback. Please try again.";
  }
}
