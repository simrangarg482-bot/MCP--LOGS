import { useEffect, useRef, useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { Sidebar } from "@/components/layout/Sidebar";
import { Topbar } from "@/components/layout/Topbar";
import { useFocusTrap } from "@/hooks/useFocusTrap";

export function AppLayout() {
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const location = useLocation();
  const mobileNavRef = useRef<HTMLDivElement>(null);

  // Close the mobile nav overlay whenever the route changes (e.g. after
  // tapping a nav link), so it doesn't stay open over the new page.
  useEffect(() => {
    setMobileNavOpen(false);
  }, [location.pathname]);

  // The off-canvas mobile nav behaves like a drawer: close on Escape, and
  // trap/restore focus the same way Modal/Drawer do.
  useEffect(() => {
    if (!mobileNavOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileNavOpen(false);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [mobileNavOpen]);

  useFocusTrap(mobileNavRef, mobileNavOpen);

  return (
    <div className="flex h-screen w-full overflow-hidden bg-background">
      {/* Persistent sidebar on desktop (lg+). Below that, the sidebar is an
          off-canvas overlay toggled by the Topbar's hamburger button. */}
      <div className="hidden lg:flex">
        <Sidebar />
      </div>

      {mobileNavOpen && (
        <div className="fixed inset-0 z-50 flex lg:hidden">
          <div
            className="absolute inset-0 bg-slate-900/40"
            onClick={() => setMobileNavOpen(false)}
            aria-hidden="true"
          />
          <div
            ref={mobileNavRef}
            role="dialog"
            aria-modal="true"
            aria-label="Navigation menu"
            tabIndex={-1}
            className="relative h-full w-[220px] max-w-[80vw] shadow-panel"
          >
            <Sidebar />
          </div>
        </div>
      )}

      <div className="flex flex-1 flex-col overflow-hidden">
        <Topbar isMobileNavOpen={mobileNavOpen} onToggleSidebar={() => setMobileNavOpen((open) => !open)} />
        <main className="flex-1 overflow-y-auto px-4 py-6 scrollbar-thin sm:px-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
