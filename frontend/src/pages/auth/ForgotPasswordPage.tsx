import { useState } from "react";
import { Link } from "react-router-dom";
import { Mail } from "lucide-react";
import { requestPasswordReset } from "@/api/auth";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { passwordErrorMessage } from "@/utils/password";

/**
 * `/forgot-password`. The confirmation is deliberately the SAME whether or not the address has
 * an account that can use a password (unknown, SSO-only, unverified and rate-limited accounts all
 * look identical to the server), and it never says "we sent you an email" as a fact.
 */
export function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (isSubmitting) return;
    setIsSubmitting(true);
    setError(null);
    try {
      await requestPasswordReset(email.trim());
      setSubmitted(email.trim());
    } catch (err) {
      setError(passwordErrorMessage(err));
    } finally {
      setIsSubmitting(false);
    }
  }

  if (submitted) {
    return (
      <div className="glass-panel rounded-2xl px-6 py-6" data-testid="forgot-password-sent">
        <div className="mb-3 flex justify-center">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-accent/10 text-accent">
            <Mail className="h-5 w-5" aria-hidden="true" />
          </div>
        </div>
        <h1 className="mb-2 text-center text-lg font-semibold text-ink">Check your email</h1>
        <p className="mb-5 text-center text-sm text-ink-muted">
          If <span className="font-medium text-ink">{submitted}</span> belongs to an account that signs in with a
          password, we&apos;ve sent a link to reset it. The link works once and expires in 30 minutes.
        </p>
        <p className="mb-4 text-center text-xs text-ink-subtle">
          Nothing arrived? Check your spam folder, make sure you used the address you signed up with, or try again in
          a minute. Accounts that sign in through single sign-on don&apos;t have a password to reset.
        </p>
        <Link to="/login" className="block text-center text-xs font-medium text-accent hover:underline">
          Back to sign in
        </Link>
      </div>
    );
  }

  return (
    <div className="glass-panel rounded-2xl px-6 py-6">
      <h1 className="mb-1 text-center text-lg font-semibold text-ink">Forgot your password?</h1>
      <p className="mb-4 text-center text-xs text-ink-subtle">
        Enter the email address you signed up with and we&apos;ll send you a link to choose a new one.
      </p>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <div>
          <label htmlFor="email" className="mb-1.5 block text-xs font-medium text-ink-muted">
            Email
          </label>
          <Input
            id="email"
            type="email"
            required
            autoFocus
            autoComplete="email"
            placeholder="you@company.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        {error && (
          <p role="alert" className="rounded-md border border-critical-border bg-critical-subtle px-3 py-2 text-xs text-critical">
            {error}
          </p>
        )}
        <Button type="submit" variant="primary" className="w-full" isLoading={isSubmitting}>
          Send reset link
        </Button>
      </form>
      <p className="mt-6 text-center text-xs text-ink-subtle">
        <Link to="/login" className="font-medium text-accent hover:underline">
          Back to sign in
        </Link>
      </p>
    </div>
  );
}
