import { NavLink, Outlet } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import { useAuth } from "@/context/AuthContext";
import { cn } from "@/utils/cn";

const SETTINGS_TABS = [
  { label: "Organization", path: "/settings/organization" },
  { label: "Project", path: "/settings/project" },
  { label: "Users", path: "/settings/users" },
  { label: "SSO", path: "/settings/sso" },
  // `core.tenancy.service.list_connectors` gates on `tenancy:manage` --
  // hide this tab for anyone lacking it, matching the real /connectors
  // nav item's gate (see routes/nav.ts).
  { label: "Connectors", path: "/settings/connectors", permission: "tenancy:manage" },
  // `core.tenancy.service.list_access_rules`/`create_access_rule`/
  // `deactivate_access_rule` all gate on `tenancy:manage` too.
  { label: "Access Rules", path: "/settings/access-rules", permission: "tenancy:manage" },
];

export function SettingsLayout() {
  const { user } = useAuth();
  const permissions = new Set(user?.permissions ?? []);
  const visibleTabs = SETTINGS_TABS.filter((tab) => !tab.permission || permissions.has(tab.permission));

  return (
    <div className="flex flex-col gap-5">
      <PageHeader title="Settings" description="Manage your organization, project, users, and integrations." />

      <div className="flex flex-col gap-6 lg:flex-row">
        <nav className="flex shrink-0 flex-row gap-1 overflow-x-auto lg:w-48 lg:flex-col lg:overflow-visible">
          {visibleTabs.map((item) => (
            <NavLink
              key={item.path}
              to={item.path}
              className={({ isActive }) =>
                cn(
                  "whitespace-nowrap rounded-md px-3 py-2 text-sm font-medium transition-colors",
                  isActive ? "bg-slate-100 text-ink" : "text-ink-muted hover:bg-slate-50 hover:text-ink",
                )
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="min-w-0 flex-1">
          <Outlet />
        </div>
      </div>
    </div>
  );
}
