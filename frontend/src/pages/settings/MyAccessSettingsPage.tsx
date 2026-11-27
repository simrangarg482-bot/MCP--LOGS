import { useQuery } from "@tanstack/react-query";
import { Check, Minus } from "lucide-react";
import { Card } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { LoadingState } from "@/components/ui/LoadingState";
import { ErrorState } from "@/components/ui/ErrorState";
import { listPermissions } from "@/api/roles";
import { useTenant } from "@/context/TenantContext";
import { useAuth } from "@/context/AuthContext";
import { groupPermissions } from "@/utils/roles";

/** Read-only "what can I do here" -- the signed-in user's roles and the
 * permissions they add up to, in plain language. Visible to every member
 * (the permission catalog it reads is not tenant data). */
export function MyAccessSettingsPage() {
  const { organization } = useTenant();
  const { user } = useAuth();
  const catalogQuery = useQuery({
    queryKey: ["permissions", organization?.id],
    queryFn: () => listPermissions(organization!.id),
    enabled: Boolean(organization),
    staleTime: 5 * 60 * 1000,
  });

  const held = new Set(user?.permissions ?? []);
  const roles = user?.roles ?? [];
  const catalog = catalogQuery.data ?? [];
  const known = new Set(catalog.map((p) => p.code));
  // Permissions the user holds that are not in the assignable catalog (e.g. an
  // internal code) are still shown, by code, so nothing is hidden from them.
  const extra = [...held].filter((code) => !known.has(code)).sort();

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <div className="border-b border-border px-4 py-3">
          <h3 className="text-sm font-semibold text-ink">My roles</h3>
          <p className="mt-0.5 text-xs text-ink-muted">
            Your access in {organization?.name ?? "this organization"} is the combination of every role below.
            Only an administrator can change it.
          </p>
        </div>
        <div className="flex flex-wrap gap-2 px-4 py-4" data-testid="my-roles">
          {roles.length === 0 ? (
            <span className="text-sm text-ink-muted">You don't hold any role in this organization.</span>
          ) : (
            roles.map((role) => (
              <Badge key={role} tone={role === "admin" ? "info" : "accent"}>
                {role}
              </Badge>
            ))
          )}
        </div>
      </Card>

      <Card>
        <div className="border-b border-border px-4 py-3">
          <h3 className="text-sm font-semibold text-ink">My permissions</h3>
          <p className="mt-0.5 text-xs text-ink-muted">{held.size} granted. Anything not ticked is not available to you.</p>
        </div>
        {catalogQuery.isLoading ? (
          <LoadingState />
        ) : catalogQuery.isError ? (
          <ErrorState onRetry={() => catalogQuery.refetch()} />
        ) : (
          <div className="flex flex-col gap-5 px-4 py-4" data-testid="my-permissions">
            {groupPermissions(catalog).map(([group, permissions]) => (
              <section key={group}>
                <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-subtle">{group}</h4>
                <ul className="flex flex-col gap-1.5">
                  {permissions.map((permission) => {
                    const granted = held.has(permission.code);
                    return (
                      <li
                        key={permission.code}
                        data-permission={permission.code}
                        data-granted={granted}
                        className="flex items-start gap-2.5 rounded-lg border border-white/10 px-3 py-2"
                      >
                        {granted ? (
                          <Check className="mt-0.5 h-4 w-4 shrink-0 text-success" aria-label="Granted" />
                        ) : (
                          <Minus className="mt-0.5 h-4 w-4 shrink-0 text-ink-subtle" aria-label="Not granted" />
                        )}
                        <span className={granted ? "" : "opacity-60"}>
                          <span className="block text-sm font-medium text-ink">{permission.label}</span>
                          <span className="block text-xs text-ink-muted">{permission.description}</span>
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </section>
            ))}
            {extra.length > 0 && (
              <section>
                <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-subtle">Other</h4>
                <div className="flex flex-wrap gap-1.5">
                  {extra.map((code) => (
                    <Badge key={code}>{code}</Badge>
                  ))}
                </div>
              </section>
            )}
          </div>
        )}
      </Card>
    </div>
  );
}
