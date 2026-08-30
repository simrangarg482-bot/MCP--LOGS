import { NavLink } from "react-router-dom";
import { Boxes, CircleHelp } from "lucide-react";
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
        "flex h-full flex-col bg-sidebar text-sidebar-text transition-[width] duration-150",
        collapsed ? "w-[68px]" : "w-[248px]",
      )}
    >
      <div className="flex h-16 items-center gap-3 border-b border-white/10 px-4">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-accent text-white shadow-lg shadow-blue-950/30">
          <Boxes className="h-5 w-5" />
        </div>
        {!collapsed && (
          <div className="min-w-0">
            <span className="block text-sm font-semibold tracking-wide text-white">EKIP</span>
            <span className="block truncate text-[10px] text-slate-400">Engineering knowledge</span>
          </div>
        )}
      </div>

      <nav aria-label="Primary navigation" className="flex-1 overflow-y-auto px-3 py-4 scrollbar-thin">
        {!collapsed && <p className="mb-2 px-2.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500">Workspace</p>}
        <ul className="flex flex-col gap-0.5">
          {PRIMARY_NAV.filter(visible).map((item) => (
            <SidebarLink key={item.path} item={item} collapsed={collapsed} />
          ))}
        </ul>

        <div className="my-4 border-t border-white/10" />
        {!collapsed && <p className="mb-2 px-2.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500">Administration</p>}

        <ul className="flex flex-col gap-0.5">
          {SETTINGS_NAV.filter(visible).map((item) => (
            <SidebarLink key={item.path} item={item} collapsed={collapsed} />
          ))}
        </ul>
      </nav>
      {!collapsed && (
        <div className="m-3 rounded-xl border border-white/10 bg-white/[0.04] p-3">
          <div className="flex items-center gap-2 text-xs font-medium text-slate-200">
            <CircleHelp className="h-3.5 w-3.5 text-blue-400" />
            Need help?
          </div>
          <p className="mt-1 text-[11px] leading-4 text-slate-400">Review connector health or ask EKIP about your systems.</p>
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
            "relative flex items-center gap-3 rounded-lg px-2.5 py-2.5 text-sm font-medium transition-colors",
            isActive ? "bg-white/10 text-white" : "text-sidebar-text hover:bg-white/[0.07] hover:text-white",
          )
        }
      >
        <Icon className="h-4 w-4 shrink-0" />
        {!collapsed && <span className="truncate">{item.label}</span>}
      </NavLink>
    </li>
  );
}
