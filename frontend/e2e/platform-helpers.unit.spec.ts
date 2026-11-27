import { expect, test } from "@playwright/test";
import {
  allowedActions,
  isPlatformSessionFailure,
  statusLabel,
  statusTone,
  suspensionNoticeFromError,
  validateOptionalText,
  validateReason,
} from "../src/utils/platform";

// Pure-logic unit tests (no browser, no backend). They run with the same `playwright test` command.

test.describe("lifecycle helpers", () => {
  test("each status offers exactly the transitions the database allows", () => {
    expect(allowedActions("onboarding")).toEqual(["activate", "suspend"]);
    expect(allowedActions("active")).toEqual(["suspend"]);
    expect(allowedActions("suspended")).toEqual(["reactivate"]);
  });

  test("a reason is mandatory, trimmed and bounded", () => {
    expect(validateReason("")).toBe("A reason is required.");
    expect(validateReason("   \n\t")).toBe("A reason is required.");
    expect(validateReason("Invoice overdue")).toBeNull();
    expect(validateReason("x".repeat(1000))).toBeNull();
    expect(validateReason("x".repeat(1001))).toContain("at most 1000");
  });

  test("optional text is bounded but may be empty", () => {
    expect(validateOptionalText("", 10, "The note")).toBeNull();
    expect(validateOptionalText("x".repeat(11), 10, "The note")).toBe("The note must be at most 10 characters.");
  });

  test("labels and tones", () => {
    expect(statusLabel("onboarding")).toBe("Onboarding");
    expect(statusLabel("suspended")).toBe("Suspended");
    expect(statusTone("active")).toBe("success");
    expect(statusTone("suspended")).toBe("critical");
    expect(statusTone("onboarding")).toBe("neutral");
  });
});

test.describe("platform session failures", () => {
  test("only platform session errors end the console session", () => {
    for (const code of ["platform.invalid_token", "platform.access_denied", "platform.session_revoked", "platform.unavailable"]) {
      expect(isPlatformSessionFailure(code)).toBe(true);
    }
    for (const code of [undefined, "platform.admin_required", "organization.already_in_status", "rate_limited.operator", "auth.session_revoked"]) {
      expect(isPlatformSessionFailure(code)).toBe(false);
    }
  });
});

test.describe("suspension notice", () => {
  test("keeps only the organization name and the customer message", () => {
    const notice = suspensionNoticeFromError({
      detail: { organization_name: "Initech", customer_message: "Call billing", internal_note: "SECRET", reason: "SECRET" },
    });
    expect(notice).toEqual({ organizationName: "Initech", customerMessage: "Call billing" });
    expect(JSON.stringify(notice)).not.toContain("SECRET");
  });

  test("tolerates a missing, null or malformed detail", () => {
    const empty = { organizationName: null, customerMessage: null };
    expect(suspensionNoticeFromError(undefined)).toEqual(empty);
    expect(suspensionNoticeFromError({ detail: null })).toEqual(empty);
    expect(suspensionNoticeFromError({ detail: "text" })).toEqual(empty);
    expect(suspensionNoticeFromError({ detail: { organization_name: 5, customer_message: ["x"] } })).toEqual(empty);
    expect(suspensionNoticeFromError({ detail: { organization_name: "  ", customer_message: "" } })).toEqual(empty);
  });
});
