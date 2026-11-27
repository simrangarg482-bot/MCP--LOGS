import { Link } from "react-router-dom";
import { ShieldOff } from "lucide-react";

/** Shown whenever the server-side kill switch (PLATFORM_ADMIN_ENABLED) is off. No form, nothing to try. */
export function PlatformUnavailable() {
  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="glass-panel w-full max-w-sm rounded-2xl px-6 py-8 text-center" role="status">
        <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-full bg-white/[0.06]">
          <ShieldOff className="h-5 w-5 text-ink-subtle" aria-hidden="true" />
        </div>
        <h1 className="text-lg font-semibold text-ink">Platform console unavailable</h1>
        <p className="mt-2 text-sm text-ink-muted">
          Platform administration is not enabled on this deployment. If you are an EKIP operator, contact the
          team that runs it.
        </p>
        <Link to="/login" className="mt-5 inline-block text-xs font-medium text-accent hover:underline">
          Back to EKIP sign in
        </Link>
      </div>
    </div>
  );
}
