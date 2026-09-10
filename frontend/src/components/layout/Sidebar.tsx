import { NavLink } from "react-router-dom";
import { Boxes } from "lucide-react";
import { cn } from "@/utils/cn";
import { PRIMARY_NAV, SETTINGS_NAV, type NavItem } from "@/routes/nav";
import { useAuth } from "@/context/AuthContext";

interface SidebarProps {
  collapsed?: boolean;
}

export function Sidebar({ collapsed }: SidebarProps) {
  const { user } = useAuth();
  const permissions = new Set(user?.permissions ?? []);
  const visible = (item: NavItem) => !item.permission || permissions.has(item.permission);

  return (
    <aside
      className={cn(
        "flex h-full flex-col border-r border-white/[0.06] bg-sidebar text-sidebar-text backdrop-blur-xl transition-[width] duration-150",
        collapsed ? "w-[68px]" : "w-[244px]",
      )}
    >
      <div className="flex h-16 items-center gap-2.5 border-b border-white/[0.06] px-4">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-accent to-info text-white shadow-glow">
          <Boxes className="h-[18px] w-[18px]" />
        </div>
        {!collapsed && (
          <div className="min-w-0 leading-tight">
            <span className="block font-display text-[13px] font-semibold tracking-tight text-white">EKIP</span>
            <span className="block truncate text-[10px] text-sidebar-text-muted">Engineering knowledge</span>
          </div>
        )}
      </div>

      <nav aria-label="Primary navigation" className="flex-1 overflow-y-auto px-2.5 py-3 scrollbar-thin">
        {!collapsed && (
          <p className="mb-1 px-2.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-sidebar-text-muted">
            Workspace
          </p>
        )}
        <ul className="flex flex-col gap-px">
          {PRIMARY_NAV.filter(visible).map((item) => (
            <SidebarLink key={item.path} item={item} collapsed={collapsed} />
          ))}
        </ul>

        <div className="my-3 h-px bg-white/[0.06]" />
        {!collapsed && (
          <p className="mb-1 px-2.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-sidebar-text-muted">
            Administration
          </p>
        )}
        <ul className="flex flex-col gap-px">
          {SETTINGS_NAV.filter(visible).map((item) => (
            <SidebarLink key={item.path} item={item} collapsed={collapsed} />
          ))}
        </ul>
      </nav>

      {!collapsed && user && (
        <div className="flex items-center gap-2.5 border-t border-white/[0.06] px-3 py-3">
          <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-white/[0.08] text-[11px] font-semibold uppercase text-white">
            {(user.name ?? "?").slice(0, 1)}
          </div>
          <div className="min-w-0 leading-tight">
            <span className="block truncate text-xs font-medium text-sidebar-text">{user.name}</span>
            <span className="block truncate text-[10px] text-sidebar-text-muted">{user.email}</span>
          </div>
        </div>
      )}
    </aside>
  );
}

function SidebarLink({
  item,
  collapsed,
}: {
  item: (typeof PRIMARY_NAV)[number];
  collapsed?: boolean;
}) {
  const Icon = item.icon;
  return (
    <li>
      <NavLink
        to={item.path}
        title={collapsed ? item.label : undefined}
        className={({ isActive }) =>
          cn(
            "group relative flex items-center gap-3 rounded-lg px-2.5 py-2 text-[13px] font-medium transition-colors",
            isActive
              ? "border border-accent-border bg-accent-subtle text-white shadow-[inset_0_1px_0_0_rgb(255_255_255/0.06)]"
              : "border border-transparent text-sidebar-text hover:bg-white/[0.05] hover:text-white",
          )
        }
      >
        {({ isActive }) => (
          <>
            <span
              className={cn(
                "absolute -left-px top-1/2 h-5 w-0.5 -translate-y-1/2 rounded-r-full bg-gradient-to-b from-accent to-info transition-opacity",
                isActive ? "opacity-100" : "opacity-0",
              )}
            />
            <Icon
              className={cn(
                "h-[17px] w-[17px] shrink-0 transition-colors",
                isActive ? "text-accent" : "text-sidebar-text-muted group-hover:text-sidebar-text",
              )}
            />
            {!collapsed && <span className="truncate">{item.label}</span>}
          </>
        )}
      </NavLink>
    </li>
  );
}
