import { useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { LoadingState } from "@/components/ui/LoadingState";
import { usePlatformAuth } from "@/context/PlatformAuthContext";
import type { ApiError } from "@/types/common";
import { PlatformUnavailable } from "./PlatformUnavailable";

export function PlatformLoginPage() {
  const { availability, operator, endedReason, login } = usePlatformAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (availability === "checking") {
    return (
      <div className="flex h-screen items-center justify-center">
        <LoadingState label="Checking platform access…" />
      </div>
    );
  }
  if (availability === "unavailable") return <PlatformUnavailable />;
  if (operator) return <Navigate to="/platform" replace />;

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await login(email.trim(), password);
      navigate("/platform", { replace: true });
    } catch (err) {
      const code = (err as ApiError)?.errorCode;
      // One message for every credential problem: the server does not say which part was wrong, and
      // neither does this page.
      setError(
        code === "rate_limited.ip" || code === "rate_limited.account"
          ? "Too many attempts. Wait a minute and try again."
          : code === "platform.unavailable"
            ? "Platform access is not available."
            : "Invalid email or password.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="glass-panel w-full max-w-sm rounded-2xl px-6 py-6">
        <div className="mb-4 flex flex-col items-center gap-2 text-center">
          <ShieldCheck className="h-6 w-6 text-accent" aria-hidden="true" />
          <h1 className="text-lg font-semibold text-ink">EKIP platform console</h1>
          <p className="text-xs text-ink-subtle">For EKIP operators only. This is not the customer sign-in.</p>
        </div>
        {endedReason && (
          <p role="status" className="mb-3 rounded-md border border-white/10 bg-white/[0.04] px-3 py-2 text-xs text-ink-muted">
            {endedReason}
          </p>
        )}
        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
          <div>
            <label htmlFor="platform-email" className="mb-1.5 block text-xs font-medium text-ink-muted">
              Operator email
            </label>
            <Input
              id="platform-email"
              type="email"
              required
              autoFocus
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="platform-password" className="mb-1.5 block text-xs font-medium text-ink-muted">
              Password
            </label>
            <Input
              id="platform-password"
              type="password"
              required
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          {error && (
            <p role="alert" className="text-xs text-critical">
              {error}
            </p>
          )}
          <Button type="submit" variant="primary" className="w-full" isLoading={submitting}>
            Sign in to console
          </Button>
        </form>
      </div>
    </div>
  );
}
