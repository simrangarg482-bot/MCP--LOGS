import type { ApiError } from "@/types/common";

/** Mirrors `app/core/auth/passwords.py` -- UX only; the server enforces and is the authority. */
export const MIN_PASSWORD_LENGTH = 12;
export const MAX_PASSWORD_LENGTH = 128;

/** First problem with a NEW password, or null if it is acceptable. Nothing is trimmed:
 * what is typed is what is checked (and what the server will hash). */
export function validateNewPassword(password: string, email?: string): string | null {
  if (password.length < MIN_PASSWORD_LENGTH) {
    return `Use at least ${MIN_PASSWORD_LENGTH} characters.`;
  }
  if (password.length > MAX_PASSWORD_LENGTH) {
    return `Use at most ${MAX_PASSWORD_LENGTH} characters.`;
  }
  if (!password.trim()) {
    return "A password can't be only spaces.";
  }
  if (email && password.trim().toLowerCase() === email.trim().toLowerCase()) {
    return "Your password can't be the same as your email address.";
  }
  return null;
}

export const PASSWORD_HINT = `${MIN_PASSWORD_LENGTH}-${MAX_PASSWORD_LENGTH} characters. A few random words make a strong, easy-to-remember password.`;

/** Sentence for an error from a reset / change / signup call. */
export function passwordErrorMessage(error: unknown): string {
  const apiError = error as Partial<ApiError> | undefined;
  switch (apiError?.errorCode) {
    case "auth.password_too_short":
      return `Use at least ${MIN_PASSWORD_LENGTH} characters.`;
    case "auth.password_too_long":
      return `Use at most ${MAX_PASSWORD_LENGTH} characters.`;
    case "auth.password_blank":
      return "A password can't be only spaces.";
    case "auth.password_same_as_email":
      return "Your password can't be the same as your email address.";
    case "auth.password_unchanged":
      return "Choose a password different from your current one.";
    case "auth.current_password_incorrect":
      return "Your current password is incorrect.";
    case "auth.no_password":
      return "This account signs in through single sign-on, so it has no password to change.";
    case "auth.reset_token_expired":
      return "This reset link has expired. Request a new one.";
    case "auth.reset_token_used":
      return "This reset link has already been used. Request a new one if you still need to reset your password.";
    case "auth.reset_token_invalid":
      return "This reset link isn't valid. Request a new one.";
    case "rate_limited.ip":
    case "rate_limited.user":
      return "Too many attempts. Please wait a minute and try again.";
    case "email.not_configured":
      return "Email isn't set up on this server, so reset links can't be sent. Contact your administrator.";
    case "auth.organization_suspended":
      return "Your organization's access has been suspended. Contact your administrator.";
    default:
      return apiError?.message || "Something went wrong. Please try again.";
  }
}
