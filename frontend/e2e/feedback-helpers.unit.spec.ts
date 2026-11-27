import { expect, test } from "@playwright/test";
import { keysToCamelCase, keysToSnakeCase } from "../src/api/caseConversion";
import {
  FEEDBACK_REASON_OPTIONS,
  MAX_FEEDBACK_COMMENT_LENGTH,
  buildFeedbackPayload,
  feedbackErrorMessage,
  isFeedbackEligible,
  shouldSubmitRating,
  validateFeedbackComment,
} from "../src/utils/feedback";

// Pure-logic unit tests (no browser, no backend). They run with the same `playwright test` command.

test.describe("when a rating click may submit", () => {
  test("a first click on either button submits", () => {
    expect(shouldSubmitRating(null, "helpful", false)).toBe(true);
    expect(shouldSubmitRating(null, "not_helpful", false)).toBe(true);
  });

  test("clicking the rating already saved does nothing (no duplicate)", () => {
    expect(shouldSubmitRating("helpful", "helpful", false)).toBe(false);
    expect(shouldSubmitRating("not_helpful", "not_helpful", false)).toBe(false);
  });

  test("clicking the other rating changes it", () => {
    expect(shouldSubmitRating("helpful", "not_helpful", false)).toBe(true);
  });

  test("nothing submits while a request is in flight", () => {
    expect(shouldSubmitRating(null, "helpful", true)).toBe(false);
    expect(shouldSubmitRating("helpful", "not_helpful", true)).toBe(false);
  });
});

test.describe("what is sent", () => {
  test("a bare rating sends no reason and no comment", () => {
    expect(buildFeedbackPayload("not_helpful", null, "")).toEqual({
      rating: "not_helpful",
      reasonCode: null,
      comment: null,
    });
  });

  test("a negative rating carries its reason and a trimmed comment", () => {
    expect(buildFeedbackPayload("not_helpful", "outdated", "  old docs \n")).toEqual({
      rating: "not_helpful",
      reasonCode: "outdated",
      comment: "old docs",
    });
  });

  test("a reason never accompanies a helpful rating", () => {
    expect(buildFeedbackPayload("helpful", "incorrect", "")).toEqual({
      rating: "helpful",
      reasonCode: null,
      comment: null,
    });
  });

  test("a whitespace-only comment is omitted, not sent blank", () => {
    expect(buildFeedbackPayload("not_helpful", null, "   \n\t").comment).toBeNull();
  });

  test("the request body uses the snake_case names the API expects, and never names a tenant or user", () => {
    const body = keysToSnakeCase(buildFeedbackPayload("not_helpful", "irrelevant_sources", "x")) as Record<
      string,
      unknown
    >;
    expect(Object.keys(body).sort()).toEqual(["comment", "rating", "reason_code"]);
    expect(body.reason_code).toBe("irrelevant_sources");
  });

  test("the response is converted back to camelCase", () => {
    const record = keysToCamelCase<{ executionId: string; reasonCode: string | null }>({
      id: "1",
      execution_id: "e1",
      reason_code: null,
    });
    expect(record.executionId).toBe("e1");
    expect(record.reasonCode).toBeNull();
  });

  test("every reason offered matches a value the API accepts", () => {
    expect(FEEDBACK_REASON_OPTIONS.map((option) => option.value).sort()).toEqual(
      ["incomplete", "incorrect", "irrelevant_sources", "no_answer_needed_more", "other", "outdated"].sort(),
    );
  });
});

test.describe("validation and eligibility", () => {
  test("the comment limit matches the backend's", () => {
    expect(MAX_FEEDBACK_COMMENT_LENGTH).toBe(2000);
    expect(validateFeedbackComment("x".repeat(2000))).toBeNull();
    expect(validateFeedbackComment("x".repeat(2001))).toContain("2000");
  });

  test("only a response with an execution id can be rated", () => {
    expect(isFeedbackEligible({ executionId: "abc" })).toBe(true);
    expect(isFeedbackEligible({ executionId: null })).toBe(false);
    expect(isFeedbackEligible({})).toBe(false);
    expect(isFeedbackEligible(undefined)).toBe(false);
  });
});

test.describe("error messages", () => {
  test("each known failure has its own plain-language message", () => {
    expect(feedbackErrorMessage({ status: 404 })).toContain("can no longer be rated");
    expect(feedbackErrorMessage({ status: 409 })).toContain("didn't complete");
    expect(feedbackErrorMessage({ status: 429 })).toContain("too quickly");
    expect(feedbackErrorMessage({ status: 422 })).toContain("couldn't be saved");
  });

  test("anything else is generic and never leaks server text", () => {
    const message = feedbackErrorMessage({ status: 500, message: "psycopg2.OperationalError: secret detail" });
    expect(message).toBe("Couldn't save your feedback. Please try again.");
    expect(feedbackErrorMessage(new Error("boom"))).toBe("Couldn't save your feedback. Please try again.");
    expect(feedbackErrorMessage(null)).toBe("Couldn't save your feedback. Please try again.");
  });
});
