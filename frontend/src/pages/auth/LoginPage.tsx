import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { useToast } from "@/context/ToastContext";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { SsoSignIn } from "@/components/domain/SsoSignIn";
import type { OrganizationSummary } from "@/types/auth";
import type { ApiError } from "@/types/common";
import { ResendVerification } from "./ResendVerification";

export function LoginPage() {
  const { login, selectOrganization } = useAuth();
  const { toast } = useToast();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  // Set only for a multi-organization user (core.auth.schemas.
  // OrganizationSelectionRequired) -- the password check already
  // succeeded, but no session exists yet until one of these is chosen.
  // Non-null is what switches this page from the password form to the
  // organization picker below.
  const [pendingSelection, setPendingSelection] = useState<{
    selectionToken: string;
    organizations: OrganizationSummary[];
  } | null>(null);
  const [selectingOrganizationId, setSelectingOrganizationId] = useState<string | null>(null);
  // Set when the backend refused login with `auth.email_not_verified` (correct
  // password, but the address was never confirmed). The refusal is the server's:
  // no tokens were issued, and this screen only offers to resend the link.
  const [unverifiedEmail, setUnverifiedEmail] = useState<string | null>(null);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setIsSubmitting(true);
    try {
      const outcome = await login({ email, password });
      if (outcome?.status === "organization_selection_required") {
        setPendingSelection({
          selectionToken: outcome.selectionToken,
          organizations: outcome.organizations,
        });
        return;
      }
      navigate("/ask");
    } catch (err) {
      if ((err as ApiError)?.errorCode === "auth.email_not_verified") {
        setUnverifiedEmail(email.trim());
        return;
      }
      const code = (err as ApiError)?.errorCode;
      if (code === "auth.organization_suspended") {
        // Every organization this person can sign in to is suspended: say so (the API client already
        // recorded the organization's customer-facing message).
        navigate("/suspended");
        return;
      }
      toast({
        variant: "error",
        title: "Sign in failed",
        description:
          code === "auth.organization_suspended"
            ? "Your organization's access has been suspended. Contact your administrator."
            : code === "rate_limited.ip"
              ? "Too many attempts. Please wait a minute and try again."
              : "Check your email and password and try again.",
      });
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleSelectOrganization(organizationId: string) {
    if (!pendingSelection) return;
    setSelectingOrganizationId(organizationId);
    try {
      await selectOrganization({
        selectionToken: pendingSelection.selectionToken,
        organizationId,
      });
      navigate("/ask");
    } catch {
      toast({
        variant: "error",
        title: "Couldn't sign in to that organization",
        description: "Please try again.",
      });
    } finally {
      setSelectingOrganizationId(null);
    }
  }

  if (unverifiedEmail) {
    return (
      <div className="glass-panel rounded-2xl px-6 py-6">
        <h1 className="mb-1 text-center text-lg font-semibold text-ink">Confirm your email first</h1>
        <p className="mb-5 text-center text-sm text-ink-muted">
          Your password is correct, but <span className="font-medium text-ink">{unverifiedEmail}</span>{" "}
          hasn&apos;t been confirmed yet. Open the link in the email we sent, or request a new one.
        </p>
        <ResendVerification email={unverifiedEmail} />
        <button
          type="button"
          className="mt-4 block w-full text-center text-xs font-medium text-ink-subtle hover:text-ink"
          onClick={() => setUnverifiedEmail(null)}
        >
          Back to sign in
        </button>
      </div>
    );
  }

  if (pendingSelection) {
    return (
      <div className="glass-panel rounded-2xl px-6 py-6">
        <h1 className="mb-1 text-center text-lg font-semibold text-ink">Choose an organization</h1>
        <p className="mb-4 text-center text-xs text-ink-subtle">
          Your account belongs to more than one organization. Pick one to continue.
        </p>
        <div className="flex flex-col gap-2">
          {pendingSelection.organizations.map((org) => (
            <Button
              key={org.id}
              type="button"
              variant="secondary"
              className="w-full justify-start"
              isLoading={selectingOrganizationId === org.id}
              disabled={selectingOrganizationId !== null && selectingOrganizationId !== org.id}
              onClick={() => handleSelectOrganization(org.id)}
            >
              {org.name}
            </Button>
          ))}
        </div>
        <button
          type="button"
          className="mt-4 block w-full text-center text-xs font-medium text-ink-subtle hover:text-ink"
          onClick={() => setPendingSelection(null)}
        >
          Back to sign in
        </button>
      </div>
    );
  }

  return (
    <div className="glass-panel rounded-2xl px-6 py-6">
      <h1 className="mb-4 text-center text-lg font-semibold text-ink">Sign in to EKIP</h1>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <div>
          <label htmlFor="email" className="mb-1.5 block text-xs font-medium text-ink-muted">
            Work email
          </label>
          <Input
            id="email"
            type="email"
            required
            autoFocus
            placeholder="you@company.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="password" className="mb-1.5 block text-xs font-medium text-ink-muted">
            Password
          </label>
          <Input
            id="password"
            type="password"
            required
            placeholder="••••••••"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <div className="mt-1.5 text-right">
            <Link to="/forgot-password" className="text-xs font-medium text-accent hover:underline">
              Forgot password?
            </Link>
          </div>
        </div>
        <Button type="submit" variant="primary" className="w-full" isLoading={isSubmitting}>
          Sign in
        </Button>
      </form>

      <div className="my-5 flex items-center gap-3 text-[11px] uppercase tracking-wide text-ink-subtle" aria-hidden="true">
        <span className="h-px flex-1 bg-border" />
        or sign in with your organization
        <span className="h-px flex-1 bg-border" />
      </div>
      <SsoSignIn />

      <p className="mt-6 text-center text-xs text-ink-subtle">
        Don't have an account?{" "}
        <Link to="/signup" className="font-medium text-accent hover:underline">
          Create one
        </Link>
      </p>
      <p className="mt-2 text-center text-xs text-ink-subtle">
        <Link to="/about" className="font-medium text-ink-muted transition-colors hover:text-ink">
          Learn what EKIP does &rarr;
        </Link>
      </p>
    </div>
  );
}
