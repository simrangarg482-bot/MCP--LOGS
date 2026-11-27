import { useState } from "react";
import { Link } from "react-router-dom";
import { CheckCircle2, XCircle } from "lucide-react";
import { resetPassword } from "@/api/auth";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import type { ApiError } from "@/types/common";
import {
  MAX_PASSWORD_LENGTH,
  MIN_PASSWORD_LENGTH,
  PASSWORD_HINT,
  passwordErrorMessage,
  validateNewPassword,
} from "@/utils/password";

type Dead = "missing" | "invalid" | "expired" | "used";

// React StrictMode (development) renders every component twice, and the second render sees the URL
// AFTER the first one stripped the fragment. So the token is captured once into this module
// variable -- in memory only, never localStorage/sessionStorage -- and every render reads it from
// there. A newly opened link (a fragment is present again) replaces it.
let capturedToken: string | null = null;

/** Read the token from the URL FRAGMENT (`/reset-password#token=...`) -- never sent to servers or
 * in `Referer` -- and remove it from the address bar immediately. */
function takeTokenFromUrl(): string | null {
  if (window.location.hash) {
    const token = new URLSearchParams(window.location.hash.replace(/^#/, "")).get("token");
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    capturedToken = token && token.length > 0 ? token : null;
  }
  return capturedToken;
}

const DEAD_COPY: Record<Dead, { title: string; body: string }> = {
  missing: { title: "This link is incomplete", body: "The reset link is missing its token. Request a new one." },
  invalid: { title: "This link isn't valid", body: "It may have been copied incorrectly. Request a new reset link." },
  expired: { title: "This link has expired", body: "Reset links last 30 minutes. Request a new one." },
  used: { title: "This link was already used", body: "Each reset link works once. If you still need to reset your password, request a new one." },
};

/**
 * `/reset-password#token=...`. Success is reached ONLY when `POST /auth/password/reset` resolved --
 * the server consuming the single-use token is the proof. No session is created; the person signs
 * in with the new password (every old session was ended by the server).
 */
export function ResetPasswordPage() {
  const [token] = useState<string | null>(takeTokenFromUrl);

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [dead, setDead] = useState<Dead | null>(token ? null : "missing");

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (isSubmitting || !token) return;
    const problem = validateNewPassword(password);
    if (problem) {
      setError(problem);
      return;
    }
    if (password !== confirm) {
      setError("The two passwords don't match.");
      return;
    }
    setIsSubmitting(true);
    setError(null);
    try {
      await resetPassword(token, password);
      capturedToken = null; // the link is spent
      setPassword("");
      setConfirm("");
      setDone(true);
    } catch (err) {
      switch ((err as ApiError)?.errorCode) {
        case "auth.reset_token_expired":
          setDead("expired");
          break;
        case "auth.reset_token_used":
          setDead("used");
          break;
        case "auth.reset_token_invalid":
          setDead("invalid");
          break;
        default:
          // A policy problem or a transient failure: the link is still usable, so stay on the form.
          setError(passwordErrorMessage(err));
      }
    } finally {
      setIsSubmitting(false);
    }
  }

  if (done) {
    return (
      <div className="glass-panel rounded-2xl px-6 py-6" data-testid="reset-success">
        <div className="mb-3 flex justify-center text-success">
          <CheckCircle2 className="h-8 w-8" aria-hidden="true" />
        </div>
        <h1 className="mb-2 text-center text-lg font-semibold text-ink">Password changed</h1>
        <p className="mb-5 text-center text-sm text-ink-muted">
          Your password was changed and you were signed out of every device. Sign in with your new password.
        </p>
        <Link to="/login" className="block">
          <Button type="button" variant="primary" className="w-full">
            Go to sign in
          </Button>
        </Link>
      </div>
    );
  }

  if (dead) {
    const copy = DEAD_COPY[dead];
    return (
      <div className="glass-panel rounded-2xl px-6 py-6" data-testid="reset-dead" data-reason={dead}>
        <div className="mb-3 flex justify-center text-critical">
          <XCircle className="h-8 w-8" aria-hidden="true" />
        </div>
        <h1 className="mb-2 text-center text-lg font-semibold text-ink">{copy.title}</h1>
        <p className="mb-5 text-center text-sm text-ink-muted">{copy.body}</p>
        <Link to="/forgot-password" className="block">
          <Button type="button" variant="primary" className="w-full">
            Request a new link
          </Button>
        </Link>
      </div>
    );
  }

  return (
    <div className="glass-panel rounded-2xl px-6 py-6">
      <h1 className="mb-1 text-center text-lg font-semibold text-ink">Choose a new password</h1>
      <p className="mb-4 text-center text-xs text-ink-subtle">You&apos;ll be signed out of every device.</p>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <div>
          <label htmlFor="new-password" className="mb-1.5 block text-xs font-medium text-ink-muted">
            New password
          </label>
          <Input
            id="new-password"
            type="password"
            required
            autoFocus
            autoComplete="new-password"
            minLength={MIN_PASSWORD_LENGTH}
            maxLength={MAX_PASSWORD_LENGTH}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <p className="mt-1 text-xs text-ink-subtle">{PASSWORD_HINT}</p>
        </div>
        <div>
          <label htmlFor="confirm-password" className="mb-1.5 block text-xs font-medium text-ink-muted">
            Confirm new password
          </label>
          <Input
            id="confirm-password"
            type="password"
            required
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        </div>
        {error && (
          <p role="alert" className="rounded-md border border-critical-border bg-critical-subtle px-3 py-2 text-xs text-critical">
            {error}
          </p>
        )}
        <Button type="submit" variant="primary" className="w-full" isLoading={isSubmitting}>
          Change password
        </Button>
      </form>
    </div>
  );
}
