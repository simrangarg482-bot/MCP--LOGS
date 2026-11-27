import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { useToast } from "@/context/ToastContext";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, PASSWORD_HINT, passwordErrorMessage, validateNewPassword } from "@/utils/password";
import type { ApiError } from "@/types/common";
import { CheckEmailPanel } from "./CheckEmailPanel";

/**
 * `/invitations/:invitationId/accept#token=...` (or the older `?token=...`) -- `invitationId` names
 * which invitation, `token` (Phase 7.5's `Invitation.token`, surfaced once
 * by `createInvitation`'s response) proves the visitor controls the invited
 * email address. Both are required; a link missing either can never
 * succeed, so that's reported immediately rather than as a failed submit.
 */
export function AcceptInvitationPage() {
  const { invitationId } = useParams<{ invitationId: string }>();
  const [searchParams] = useSearchParams();
  // Links are emailed as `/invitations/<id>/accept#token=...` (the fragment never
  // reaches servers, logs or Referer headers); links issued before that change
  // carry `?token=...`. Both work. The fragment token is read once, on first
  // render, and then removed from the address bar.
  const [token] = useState(() => {
    const fromHash = new URLSearchParams(window.location.hash.replace(/^#/, "")).get("token");
    return fromHash ?? searchParams.get("token") ?? "";
  });
  useEffect(() => {
    if (window.location.hash.includes("token=")) {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
  }, []);
  const { acceptInvitation } = useAuth();
  const { toast } = useToast();
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  // Accepting only STARTS acceptance: the invitation token is visible to the
  // inviting admin, so it is not proof of mailbox control. The server emails a
  // confirmation link to the invited address and finishes the acceptance when
  // it is opened -- no session is issued here.
  const [started, setStarted] = useState(false);
  const [accountExists, setAccountExists] = useState(false);

  if (!invitationId || !token) {
    return (
      <div className="glass-panel rounded-2xl px-6 py-6">
        <h1 className="mb-2 text-center text-lg font-semibold text-ink">Invalid invitation link</h1>
        <p className="text-center text-sm text-ink-muted">
          This link is missing its invitation token. Ask whoever invited you to send it again.
        </p>
      </div>
    );
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    const problem = validateNewPassword(password);
    if (problem) {
      toast({ variant: "error", title: "Choose a stronger password", description: problem });
      return;
    }
    setIsSubmitting(true);
    try {
      await acceptInvitation(invitationId!, { token, password, displayName: displayName || undefined });
      setStarted(true);
    } catch (err) {
      const apiError = err as ApiError;
      if (apiError?.errorCode === "invitation.account_exists") {
        setAccountExists(true);
        return;
      }
      const message = apiError?.errorCode?.startsWith("auth.password_")
        ? passwordErrorMessage(apiError)
        : apiError?.message || "This invitation link may be invalid, expired, or already used.";
      toast({ variant: "error", title: "Couldn't accept invitation", description: message });
    } finally {
      setIsSubmitting(false);
    }
  }

  if (started) {
    return <CheckEmailPanel purpose="finish accepting your invitation" />;
  }

  if (accountExists) {
    return (
      <div className="glass-panel rounded-2xl px-6 py-6 text-center">
        <h1 className="mb-2 text-lg font-semibold text-ink">You already have an account</h1>
        <p className="mb-5 text-sm text-ink-muted">
          An account already exists for the invited email address, so this link can&apos;t create a
          new one. Sign in with your existing account instead.
        </p>
        <Link to="/login" className="text-sm font-medium text-accent hover:underline">
          Go to sign in
        </Link>
      </div>
    );
  }

  return (
    <div className="glass-panel rounded-2xl px-6 py-6">
      <h1 className="mb-4 text-center text-lg font-semibold text-ink">Accept your invitation</h1>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <div>
          <label htmlFor="displayName" className="mb-1.5 block text-xs font-medium text-ink-muted">
            Your name
          </label>
          <Input
            id="displayName"
            autoFocus
            placeholder="Jane Doe"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="password" className="mb-1.5 block text-xs font-medium text-ink-muted">
            Choose a password
          </label>
          <Input
            id="password"
            type="password"
            required
            minLength={MIN_PASSWORD_LENGTH}
            maxLength={MAX_PASSWORD_LENGTH}
            autoComplete="new-password"
            placeholder={`At least ${MIN_PASSWORD_LENGTH} characters`}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <p className="mt-1 text-xs text-ink-subtle">{PASSWORD_HINT}</p>
        </div>
        <Button type="submit" variant="primary" className="w-full" isLoading={isSubmitting}>
          Accept invitation
        </Button>
      </form>

      <p className="mt-6 text-center text-xs text-ink-subtle">
        Already have an account?{" "}
        <Link to="/login" className="font-medium text-accent hover:underline">
          Sign in
        </Link>
      </p>
    </div>
  );
}
