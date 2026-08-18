import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldPlus } from "lucide-react";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Modal } from "@/components/ui/Modal";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { DataTable, type DataTableColumn } from "@/components/data/DataTable";
import { createAccessRule, deactivateAccessRule, listAccessRules } from "@/api/tenancy";
import type { AccessRule, AccessRuleType } from "@/types/tenancy";
import type { ApiError } from "@/types/common";
import { useTenant } from "@/context/TenantContext";
import { useToast } from "@/context/ToastContext";
import { useAuth } from "@/context/AuthContext";
import { formatDateTime } from "@/utils/date";
import { titleCase } from "@/utils/format";

// Matches `app.core.tenancy.schemas.AccessRuleType` exactly -- the real
// backend rejects anything else with a 422. There is no "email" rule type.
const RULE_TYPES: AccessRuleType[] = ["domain", "group"];

function CreateAccessRuleModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { organization } = useTenant();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [ruleType, setRuleType] = useState<AccessRuleType>("domain");
  const [value, setValue] = useState("");
  const [grantsRole, setGrantsRole] = useState("");

  const createMutation = useMutation({
    mutationFn: () => {
      if (!organization) throw new Error("No organization selected");
      return createAccessRule(organization.id, { ruleType, value, grantsRole });
    },
    onSuccess: () => {
      toast({ variant: "success", title: "Access rule created" });
      queryClient.invalidateQueries({ queryKey: ["access-rules"] });
      setValue("");
      setGrantsRole("");
      onClose();
    },
    onError: (error: ApiError) => {
      toast({
        variant: "error",
        title: "Failed to create access rule",
        description: error.errorCode === "role.not_found" ? `No role named "${grantsRole}" exists.` : error.message,
      });
    },
  });

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Create access rule"
      description="Automatically grant a role to anyone who signs in with a matching email domain or identity-provider group."
    >
      <form
        className="flex flex-col gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          createMutation.mutate();
        }}
      >
        <label className="flex flex-col gap-1 text-xs font-medium text-ink-muted">
          Rule type
          <Select value={ruleType} onChange={(event) => setRuleType(event.target.value as AccessRuleType)}>
            {RULE_TYPES.map((type) => (
              <option key={type} value={type}>
                {titleCase(type)}
              </option>
            ))}
          </Select>
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-ink-muted">
          {ruleType === "domain" ? "Email domain" : "Identity-provider group"}
          <Input
            required
            placeholder={ruleType === "domain" ? "acme.com" : "engineering"}
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-ink-muted">
          Grants role
          <Input
            required
            placeholder="e.g. engineer"
            value={grantsRole}
            onChange={(event) => setGrantsRole(event.target.value)}
          />
          <span className="text-xs font-normal text-ink-subtle">
            Exact role name as configured for your organization. There is no role catalog to pick from here.
          </span>
        </label>
        <div className="mt-2 flex justify-end gap-2">
          <Button type="button" variant="secondary" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="submit"
            variant="primary"
            size="sm"
            isLoading={createMutation.isPending}
            disabled={!value || !grantsRole}
          >
            Create rule
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function AccessRulesSettingsPage() {
  const { organization } = useTenant();
  const { user } = useAuth();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  // Mirrors `core.tenancy.service.create_access_rule`/`list_access_rules`/
  // `deactivate_access_rule`'s real `tenancy:manage` gate -- UX only, the
  // backend re-checks regardless.
  const canManage = Boolean(user?.permissions.includes("tenancy:manage"));
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [deactivateTarget, setDeactivateTarget] = useState<AccessRule | null>(null);

  const rulesQuery = useQuery({
    queryKey: ["access-rules", organization?.id],
    queryFn: () => listAccessRules(organization!.id),
    enabled: Boolean(organization) && canManage,
  });

  const deactivateMutation = useMutation({
    mutationFn: (ruleId: string) => deactivateAccessRule(ruleId),
    onSuccess: () => {
      toast({ variant: "success", title: "Access rule deactivated" });
      queryClient.invalidateQueries({ queryKey: ["access-rules"] });
      setDeactivateTarget(null);
    },
    onError: () => {
      toast({ variant: "error", title: "Failed to deactivate access rule" });
      setDeactivateTarget(null);
    },
  });

  const columns: DataTableColumn<AccessRule>[] = [
    {
      key: "ruleType",
      header: "Type",
      render: (row) => <Badge tone="neutral">{titleCase(row.ruleType)}</Badge>,
    },
    {
      key: "value",
      header: "Match",
      render: (row) => <span className="font-mono text-xs text-ink">{row.value}</span>,
    },
    {
      key: "grantsRoleId",
      header: "Grants role",
      render: (row) => (
        // The backend has no "list roles" endpoint to resolve this id to a
        // display name -- shown as-is rather than inventing a lookup.
        <span className="font-mono text-xs text-ink-muted" title={row.grantsRoleId}>
          {row.grantsRoleId}
        </span>
      ),
    },
    {
      key: "isActive",
      header: "Status",
      render: (row) => <Badge tone={row.isActive ? "success" : "neutral"}>{row.isActive ? "Active" : "Inactive"}</Badge>,
    },
    {
      key: "updatedAt",
      header: "Updated",
      render: (row) => formatDateTime(row.updatedAt),
    },
    {
      key: "actions",
      header: "",
      render: (row) =>
        row.isActive ? (
          <Button
            size="sm"
            variant="secondary"
            disabled={!canManage}
            title={canManage ? undefined : "Requires the tenancy:manage permission"}
            onClick={() => setDeactivateTarget(row)}
          >
            Deactivate
          </Button>
        ) : (
          // There is no reactivate endpoint -- an inactive rule stays
          // inactive; the UI must not offer to undo this.
          <span className="text-xs text-ink-subtle">No further actions</span>
        ),
    },
  ];

  if (!canManage) {
    return (
      <Card>
        <div className="px-4 py-3">
          <h3 className="text-sm font-semibold text-ink">Access rules</h3>
          <p className="mt-2 rounded-md border border-border bg-slate-50 px-3 py-2 text-xs text-ink-muted">
            You need the <span className="font-medium text-ink">tenancy:manage</span> permission to view or manage
            access rules.
          </p>
        </div>
      </Card>
    );
  }

  return (
    <Card>
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div>
          <h3 className="text-sm font-semibold text-ink">Access rules</h3>
          <p className="text-xs text-ink-muted">Automatically assign roles based on email domain or IdP group.</p>
        </div>
        <Button size="sm" variant="primary" className="gap-1.5" onClick={() => setIsCreateOpen(true)}>
          <ShieldPlus className="h-3.5 w-3.5" />
          New rule
        </Button>
      </div>
      <DataTable
        columns={columns}
        rows={rulesQuery.data ?? []}
        rowKey={(row) => row.id}
        isLoading={rulesQuery.isLoading}
        isError={rulesQuery.isError}
        onRetry={() => rulesQuery.refetch()}
        emptyTitle="No access rules configured"
        emptyDescription="Everyone must be invited individually until a domain or group rule exists."
      />

      <CreateAccessRuleModal open={isCreateOpen} onClose={() => setIsCreateOpen(false)} />

      <ConfirmDialog
        open={deactivateTarget !== null}
        title="Deactivate this access rule?"
        description={
          deactivateTarget
            ? `New sign-ins matching "${deactivateTarget.value}" will no longer be auto-assigned a role. There is no way to reactivate this rule -- you would need to create a new one.`
            : undefined
        }
        confirmLabel="Deactivate"
        destructive
        isLoading={deactivateMutation.isPending}
        onConfirm={() => deactivateTarget && deactivateMutation.mutate(deactivateTarget.id)}
        onCancel={() => setDeactivateTarget(null)}
      />
    </Card>
  );
}
