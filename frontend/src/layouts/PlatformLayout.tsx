import { Link, Navigate, Outlet } from "react-router-dom";
import { Building2, LogOut, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { LoadingState } from "@/components/ui/LoadingState";
import { PlatformAuthProvider, usePlatformAuth } from "@/context/PlatformAuthContext";
import { PlatformUnavailable } from "@/pages/platform/PlatformUnavailable";

/** Provider boundary for everything under /platform. Kept out of the tenant App tree on purpose, so no
 *  tenant page ever loads, reads or even asks about platform state. */
export function PlatformRoot() {
  return (
    <PlatformAuthProvider>
      <Outlet />
    </PlatformAuthProvider>
  );
}

/**
 * Guards the operator console. This is a convenience for the UI only: every request is authorized by the
 * server with a platform token that tenant sessions do not have, so a tenant user who types /platform
 * into the address bar just sees the sign-in page (and cannot sign in without an operator grant).
 */
export function PlatformGate() {
  const { availability, operator } = usePlatformAuth();
  if (availability === "checking") {
    return (
      <div className="flex h-screen items-center justify-center">
        <LoadingState label="Checking platform access…" />
      </div>
    );
  }
  if (availability === "unavailable") return <PlatformUnavailable />;
  if (!operator) return <Navigate to="/platform/login" replace />;
  return <PlatformLayout />;
}

function PlatformLayout() {
  const { operator, logout } = usePlatformAuth();
  return (
    <div className="min-h-screen bg-transparent">
      <header className="border-b border-white/10 bg-black/30 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3">
          <div className="flex items-center gap-6">
            <Link to="/platform" className="flex items-center gap-2 font-display text-sm font-semibold text-ink">
              <ShieldCheck className="h-4 w-4 text-accent" aria-hidden="true" />
              EKIP Platform Console
            </Link>
            <nav aria-label="Platform" className="flex items-center gap-3 text-sm">
              <Link to="/platform" className="flex items-center gap-1.5 text-ink-muted hover:text-ink">
                <Building2 className="h-3.5 w-3.5" aria-hidden="true" />
                Organizations
              </Link>
            </nav>
          </div>
          <div className="flex items-center gap-3">
            <span className="hidden text-xs text-ink-subtle sm:inline" data-testid="platform-operator">
              {operator?.email} · {operator?.role}
            </span>
            <Button size="sm" variant="secondary" onClick={logout}>
              <LogOut className="h-3.5 w-3.5" aria-hidden="true" />
              Sign out
            </Button>
          </div>
        </div>
      </header>
      <main id="main-content" className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-6">
        <Outlet />
      </main>
    </div>
  );
}
