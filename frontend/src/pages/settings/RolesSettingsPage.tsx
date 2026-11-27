import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Lock, Plus } from "lucide-react";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Modal } from "@/components/ui/Modal";
import { Input } from "@/components/ui/Input";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { EmptyState } from "@/components/ui/EmptyState";
import { DataTable, type DataTableColumn } from "@/components/data/DataTable";
import { PermissionPicker } from "@/components/domain/PermissionPicker";
import { createRole, deleteRole, listPermissions, listRoles, updateRole } from "@/api/roles";
import type { PermissionInfo, Role } from "@/types/roles";
import { useTenant } from "@/context/TenantContext";
import { useToast } from "@/context/ToastContext";
import { useAuth } from "@/context/AuthContext";
import { ADMIN_PERMISSION, roleErrorMessage } from "@/utils/roles";

type EditorState = { mode: "create" } | { mode: "edit"; role: Role } | { mode: "view"; role: Role } | null;

interface RoleEditorModalProps {
  state: EditorState;
  catalog: PermissionInfo[];
  myPermissions: string[];
  onClose: () => void;
}

/** Create / edit / (read-only) view of one role. */
function RoleEditorModal({ state, catalog, myPermissions, onClose }: RoleEditorModalProps) {
  const { organization } = useTenant();
  const { toast } = useToast();
  const { refreshUser } = useAuth();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [permissions, setPermissions] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const role = state && state.mode !== "create" ? state.role : null;
  const readOnly = state?.mode === "view";

  // Re-seed the form each time the dialog opens for a different role.
  useEffect(() => {
    if (!state) return;
    setName(role?.name ?? "");
    setDescription(role?.description ?? "");
    setPermissions(role?.permissions ?? []);
    setError(null);
  }, [state, role]);

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (!organization) throw new Error("No organization selected");
      const body = { name: name.trim(), description: description.trim() || null, permissions };
      return state?.mode === "edit" ? updateRole(organization.id, state.role.id, body) : createRole(organization.id, body);
    },
    onSuccess: async () => {
      toast({ variant: "success", title: state?.mode === "edit" ? "Role updated" : "Role created" });
      await queryClient.invalidateQueries({ queryKey: ["roles"] });
      await queryClient.invalidateQueries({ queryKey: ["users"] });
      // Editing a role can change what *this* user may do.
      await refreshUser().catch(() => undefined);
      onClose();
    },
    onError: (err) => setError(roleErrorMessage(err)),
  });

  if (!state) return null;

  const title = state.mode === "create" ? "New role" : readOnly ? `${role?.name} (built-in)` : `Edit ${role?.name}`;
  const losingAdmin = state.mode === "edit" && role?.permissions.includes(ADMIN_PERMISSION) && !permissions.includes(ADMIN_PERMISSION);
  const grantingAdmin = permissions.includes(ADMIN_PERMISSION) && !(role?.permissions ?? []).includes(ADMIN_PERMISSION);

  return (
    <Modal
      open
      onClose={onClose}
      title={title}
      description={
        readOnly
          ? "Built-in roles are shared by every organization and can't be changed."
          : "Choose exactly what members with this role are allowed to do."
      }
      className="max-w-2xl"
    >
      <form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          saveMutation.mutate();
        }}
      >
        {readOnly && (
          <div className="flex items-center gap-2 rounded-lg border border-white/10 bg-white/[0.04] px-3 py-2 text-xs text-ink-muted">
            <Lock className="h-3.5 w-3.5 shrink-0" />
            Read-only. Create a custom role if you need a different set of permissions.
          </div>
        )}
        <label className="flex flex-col gap-1 text-xs font-medium text-ink-muted">
          Role name
          <Input
            required
            minLength={2}
            maxLength={64}
            placeholder="e.g. Incident commander"
            value={name}
            readOnly={readOnly}
            onChange={(event) => setName(event.target.value)}
            aria-label="Role name"
          />
          <span className="text-xs font-normal text-ink-subtle">
            Letters, numbers, spaces, "-", "_" and "." — unique within your organization.
          </span>
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-ink-muted">
          Description (optional)
          <Input
            maxLength={300}
            placeholder="What is this role for?"
            value={description}
            readOnly={readOnly}
            onChange={(event) => setDescription(event.target.value)}
            aria-label="Role description"
          />
        </label>
        <div>
          <p className="mb-2 text-xs font-medium text-ink-muted">
            Permissions <span className="text-ink-subtle">({permissions.length} selected)</span>
          </p>
          <PermissionPicker
            catalog={catalog}
            value={permissions}
            onChange={setPermissions}
            readOnly={readOnly}
            grantable={myPermissions}
          />
        </div>
        {grantingAdmin && !readOnly && (
          <p className="flex items-start gap-2 rounded-lg border border-warning-border bg-warning-subtle px-3 py-2 text-xs text-warning">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            “Manage organization” makes everyone with this role an administrator of the organization.
          </p>
        )}
        {losingAdmin && (
          <p className="flex items-start gap-2 rounded-lg border border-warning-border bg-warning-subtle px-3 py-2 text-xs text-warning">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            Members with this role will lose administrator access. EKIP refuses the change if it would leave the
            organization without any administrator.
          </p>
        )}
        {error && (
          <p role="alert" className="rounded-lg border border-critical-border bg-critical-subtle px-3 py-2 text-xs text-critical">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" size="sm" onClick={onClose}>
            {readOnly ? "Close" : "Cancel"}
          </Button>
          {!readOnly && (
            <Button type="submit" variant="primary" size="sm" isLoading={saveMutation.isPending} disabled={name.trim().length < 2}>
              {state.mode === "create" ? "Create role" : "Save changes"}
            </Button>
          )}
        </div>
      </form>
    </Modal>
  );
}

export function RolesSettingsPage() {
  const { organization } = useTenant();
  const { user } = useAuth();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const myPermissions = user?.permissions ?? [];
  // UX only: the backend requires `tenancy:manage` for every role operation.
  const canManage = myPermissions.includes(ADMIN_PERMISSION);
  const [editor, setEditor] = useState<EditorState>(null);
  const [deleteTarget, setDeleteTarget] = useState<Role | null>(null);

  const rolesQuery = useQuery({
    queryKey: ["roles", organization?.id],
    queryFn: () => listRoles(organization!.id),
    enabled: Boolean(organization) && canManage,
  });
  const catalogQuery = useQuery({
    queryKey: ["permissions", organization?.id],
    queryFn: () => listPermissions(organization!.id),
    enabled: Boolean(organization),
    staleTime: 5 * 60 * 1000,
  });

  const deleteMutation = useMutation({
    mutationFn: (roleId: string) => deleteRole(organization!.id, roleId),
    onSuccess: () => {
      toast({ variant: "success", title: "Role deleted" });
      queryClient.invalidateQueries({ queryKey: ["roles"] });
      setDeleteTarget(null);
    },
    onError: (error) => {
      toast({ variant: "error", title: "Couldn't delete role", description: roleErrorMessage(error) });
      setDeleteTarget(null);
    },
  });

  if (!canManage) {
    return (
      <Card>
        <EmptyState
          title="Roles & permissions are managed by administrators"
          description="You need the “Manage organization” permission to view or change roles. You can see your own access under “My roles”."
        />
      </Card>
    );
  }

  const catalog = catalogQuery.data ?? [];

  const columns: DataTableColumn<Role>[] = [
    {
      key: "name",
      header: "Role",
      render: (row) => (
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium text-ink">{row.name}</span>
            {row.isBuiltin ? (
              <Badge tone="info">
                <Lock className="h-3 w-3" /> Built-in
              </Badge>
            ) : (
              <Badge tone="accent">Custom</Badge>
            )}
          </div>
          {row.description && <p className="mt-0.5 text-xs text-ink-muted">{row.description}</p>}
        </div>
      ),
    },
    {
      key: "permissions",
      header: "Permissions",
      render: (row) => (
        <span title={row.permissions.join(", ")}>
          {row.permissions.length} of {catalog.length || row.permissions.length}
        </span>
      ),
    },
    { key: "memberCount", header: "Members", render: (row) => row.memberCount },
    {
      key: "actions",
      header: "",
      render: (row) =>
        row.isBuiltin ? (
          <Button size="sm" variant="secondary" onClick={() => setEditor({ mode: "view", role: row })}>
            View
          </Button>
        ) : (
          <div className="flex justify-end gap-2">
            <Button size="sm" variant="secondary" onClick={() => setEditor({ mode: "edit", role: row })}>
              Edit
            </Button>
            <Button
              size="sm"
              variant="danger"
              disabled={row.memberCount > 0}
              title={row.memberCount > 0 ? "Remove this role from its members first" : undefined}
              onClick={() => setDeleteTarget(row)}
            >
              Delete
            </Button>
          </div>
        ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div>
            <h3 className="text-sm font-semibold text-ink">Roles & permissions</h3>
            <p className="mt-0.5 text-xs text-ink-muted">
              A member's access is the combination of all their roles. Changes apply on their next request.
            </p>
          </div>
          <Button
            size="sm"
            variant="primary"
            className="shrink-0 gap-1.5"
            disabled={catalogQuery.isLoading || catalogQuery.isError}
            onClick={() => setEditor({ mode: "create" })}
          >
            <Plus className="h-3.5 w-3.5" />
            New role
          </Button>
        </div>
        <DataTable
          columns={columns}
          rows={rolesQuery.data ?? []}
          rowKey={(row) => row.id}
          isLoading={rolesQuery.isLoading}
          isError={rolesQuery.isError}
          onRetry={() => rolesQuery.refetch()}
          emptyTitle="No roles yet"
        />
      </Card>

      <RoleEditorModal state={editor} catalog={catalog} myPermissions={myPermissions} onClose={() => setEditor(null)} />

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete this role?"
        description={
          deleteTarget
            ? `"${deleteTarget.name}" will be permanently removed. This only works while no member, invitation or access rule uses it.`
            : undefined
        }
        confirmLabel="Delete role"
        destructive
        isLoading={deleteMutation.isPending}
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
