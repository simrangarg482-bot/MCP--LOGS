import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Menu, User as UserIcon } from "lucide-react";
import { useAuth } from "@/context/AuthContext";
import { SearchBar } from "@/components/data/SearchBar";
import { DropdownMenu } from "@/components/ui/DropdownMenu";
import { PRIMARY_NAV, SETTINGS_NAV } from "@/routes/nav";
import { TenantSwitcher } from "./TenantSwitcher";

interface TopbarProps {
  isMobileNavOpen: boolean;
  onToggleSidebar: () => void;
}

export function Topbar({ isMobileNavOpen, onToggleSidebar }: TopbarProps) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [query, setQuery] = useState("");
  const searchRef = useRef<HTMLDivElement>(null);
  const currentRoute = [...PRIMARY_NAV, ...SETTINGS_NAV]
    .filter((item) => location.pathname === item.path || location.pathname.startsWith(`${item.path}/`))
    .sort((a, b) => b.path.length - a.path.length)[0];

  useEffect(() => {
    const handleShortcut = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (event.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) {
        event.preventDefault();
        searchRef.current?.querySelector("input")?.focus();
      }
    };
    document.addEventListener("keydown", handleShortcut);
    return () => document.removeEventListener("keydown", handleShortcut);
  }, []);

  function handleSubmitSearch(event: React.FormEvent) {
    event.preventDefault();
    if (query.trim()) navigate(`/search?q=${encodeURIComponent(query.trim())}`);
  }

  return (
    <header className="flex h-16 shrink-0 items-center gap-3 border-b border-border bg-surface/95 px-4 backdrop-blur sm:px-6">
      <button type="button" onClick={onToggleSidebar} aria-label="Toggle sidebar" aria-expanded={isMobileNavOpen} className="rounded-md p-1.5 text-ink-muted hover:bg-slate-100 hover:text-ink lg:hidden">
        <Menu className="h-4 w-4" />
      </button>
      <div className="hidden min-w-0 lg:block"><TenantSwitcher /></div>
      <div className="hidden h-6 w-px bg-border xl:block" />
      <p className="hidden min-w-0 truncate text-xs font-medium text-ink-muted xl:block">{currentRoute?.label ?? "Workspace"}</p>
      <form onSubmit={handleSubmitSearch} className="mx-auto max-w-xl flex-1">
        <div ref={searchRef} className="relative">
          <SearchBar value={query} onChange={setQuery} placeholder="Search incidents, knowledge, and evidence…" />
          {!query && <kbd className="pointer-events-none absolute right-2.5 top-1/2 hidden -translate-y-1/2 rounded border border-border bg-slate-50 px-1.5 py-0.5 text-[10px] text-ink-subtle sm:block">/</kbd>}
        </div>
      </form>
      <DropdownMenu
        trigger={
          <button type="button" aria-label={user?.name ? `Account menu for ${user.name}` : "Account menu"} className="flex items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-slate-100">
            <div className="flex h-8 w-8 items-center justify-center rounded-full bg-accent-subtle text-accent"><UserIcon className="h-3.5 w-3.5" /></div>
            <span className="hidden max-w-32 truncate text-sm font-medium text-ink sm:inline">{user?.name}</span>
          </button>
        }
        items={[
          { label: "Settings", onSelect: () => navigate("/settings") },
          { label: "Sign out", onSelect: () => void logout(), destructive: true },
        ]}
      />
    </header>
  );
}
