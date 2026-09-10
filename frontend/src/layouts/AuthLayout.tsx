import { Link, Outlet } from "react-router-dom";
import { Boxes } from "lucide-react";
import { FluidParticlesBackground } from "@/components/ui/fluid-particles-background";

export function AuthLayout() {
  return (
    <div className="relative flex min-h-screen items-center justify-center px-4 py-10">
      <FluidParticlesBackground asBackground particleCount={1100} />
      <div className="w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center gap-3 text-center">
          <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-gradient-to-br from-accent to-info text-white shadow-glow">
            <Boxes className="h-5 w-5" />
          </div>
          <div>
            <p className="font-display text-lg font-semibold text-gradient">EKIP</p>
            <p className="text-sm text-ink-muted">Enterprise Knowledge Incident Intelligence</p>
          </div>
        </div>
        <main>
          <Outlet />
        </main>
        <p className="mt-6 text-center text-xs text-ink-subtle">
          <Link to="/about" className="font-medium text-ink-muted transition-colors hover:text-ink">
            What is EKIP? &rarr;
          </Link>
        </p>
      </div>
    </div>
  );
}
