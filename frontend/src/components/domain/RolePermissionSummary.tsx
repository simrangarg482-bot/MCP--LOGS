import { Badge } from "@/components/ui/Badge";
import { permissionLabel } from "@/utils/roles";
import type { PermissionInfo } from "@/types/roles";

interface RolePermissionSummaryProps {
  heading: string;
  codes: readonly string[];
  catalog: PermissionInfo[];
  emptyText?: string;
}

/** A compact "this grants:" list of permission labels (code on hover). */
export function RolePermissionSummary({ heading, codes, catalog, emptyText = "No permissions." }: RolePermissionSummaryProps) {
  return (
    <div className="rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2.5" data-testid="role-permission-summary">
      <p className="mb-1.5 text-xs font-medium text-ink-muted">{heading}</p>
      {codes.length === 0 ? (
        <p className="text-xs text-ink-subtle">{emptyText}</p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {codes.map((code) => (
            <Badge key={code} tone={code === "tenancy:manage" ? "warning" : "neutral"} title={code}>
              {permissionLabel(catalog, code)}
            </Badge>
          ))}
        </div>
      )}
    </div>
  );
}
