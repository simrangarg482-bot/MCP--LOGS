import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { useToast } from "@/context/ToastContext";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import type { ApiError } from "@/types/common";

/**
 * `/invitations/:invitationId/accept?token=...` -- `invitationId` names
 * which invitation, `token` (Phase 7.5's `Invitation.token`, surfaced once
 * by `createInvitation`'s response) proves the visitor controls the invited
 * email address. Both are required; a link missing either can never
 * succeed, so that's reported immediately rather than as a failed submit.
 */
export function AcceptInvitationPage() {
  const { invitationId } = useParams<{ invitationId: string }>();
  const [searchParams] = useSearchParams();
  const token = searchParams.get("token") ?? "";
  const { acceptInvitation } = useAuth();
  const { toast } = useToast();
  const navigate = useNavigate();
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  if (!invitationId || !token) {
    return (
      <div className="rounded-lg border border-border bg-surface px-6 py-6 shadow-panel">
        <h1 className="mb-2 text-center text-lg font-semibold text-ink">Invalid invitation link</h1>
        <p className="text-center text-sm text-ink-muted">
          This link is missing its invitation token. Ask whoever invited you to send it again.
        </p>
      </div>
    );
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setIsSubmitting(true);
    try {
      await acceptInvitation(invitationId!, { token, password, displayName: displayName || undefined });
      navigate("/ask");
    } catch (err) {
      const apiError = err as ApiError;
      const message = apiError?.message || "This invitation link may be invalid, expired, or already used.";
      toast({ variant: "error", title: "Couldn't accept invitation", description: message });
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="rounded-lg border border-border bg-surface px-6 py-6 shadow-panel">
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
            minLength={8}
            placeholder="At least 8 characters"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
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
