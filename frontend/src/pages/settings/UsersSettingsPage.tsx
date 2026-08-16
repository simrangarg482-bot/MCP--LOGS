import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { UserPlus } from "lucide-react";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Modal } from "@/components/ui/Modal";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { DataTable, type DataTableColumn } from "@/components/data/DataTable";
import { createInvitation, listOrgUsers } from "@/api/tenancy";
import type { OrgUser, UserRole } from "@/types/tenancy";
import { useTenant } from "@/context/TenantContext";
import { useToast } from "@/context/ToastContext";
import { useAuth } from "@/context/AuthContext";
import { formatRelativeTime } from "@/utils/date";
import { titleCase } from "@/utils/format";

const ROLE_TONE = { owner: "accent", admin: "info", member: "neutral", viewer: "neutral" } as const;
const INVITABLE_ROLES: UserRole[] = ["admin", "member", "viewer"];

function InviteUserModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { organization } = useTenant();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<UserRole>("member");

  const inviteMutation = useMutation({
    mutationFn: () => {
      if (!organization) throw new Error("No organization selected");
      return createInvitation(organization.id, { email, grantsRole: role });
    },
    onSuccess: () => {
      toast({ variant: "success", title: `Invitation sent to ${email}` });
      queryClient.invalidateQueries({ queryKey: ["users"] });
      setEmail("");
      setRole("member");
      onClose();
    },
    onError: () => {
      toast({ variant: "error", title: "Failed to send invitation" });
    },
  });

  return (
    <Modal open={open} onClose={onClose} title="Invite a user" description="They'll receive access once they accept.">
      <form
        className="flex flex-col gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          inviteMutation.mutate();
        }}
      >
        <label className="flex flex-col gap-1 text-xs font-medium text-ink-muted">
          Email
          <Input
            type="email"
            required
            placeholder="engineer@company.com"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-ink-muted">
          Role
          <Select value={role} onChange={(event) => setRole(event.target.value as UserRole)}>
            {INVITABLE_ROLES.map((r) => (
              <option key={r} value={r}>
                {titleCase(r)}
              </option>
            ))}
          </Select>
        </label>
        <div className="mt-2 flex justify-end gap-2">
          <Button type="button" variant="secondary" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" size="sm" disabled={inviteMutation.isPending || !email}>
            {inviteMutation.isPending ? "Sending…" : "Send invitation"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function UsersSettingsPage() {
  const { organization } = useTenant();
  const { user } = useAuth();
  // Mirrors the real gate `core.tenancy.service.create_invitation` enforces
  // -- UX only, the backend re-checks regardless (see types/auth.ts's
  // AuthUser.permissions docstring).
  const canInvite = Boolean(user?.permissions.includes("tenancy:manage"));
  const usersQuery = useQuery({
    queryKey: ["users", organization?.id],
    queryFn: () => listOrgUsers(organization!.id),
    enabled: Boolean(organization),
  });
  const [isInviteOpen, setIsInviteOpen] = useState(false);

  const columns: DataTableColumn<OrgUser>[] = [
    {
      key: "name",
      header: "User",
      render: (row) => (
        <div>
          <p className="font-medium text-ink">{row.name}</p>
          <p className="text-xs text-ink-muted">{row.email}</p>
        </div>
      ),
    },
    { key: "role", header: "Role", render: (row) => <Badge tone={ROLE_TONE[row.role]}>{titleCase(row.role)}</Badge> },
    {
      key: "status",
      header: "Status",
      render: (row) => <Badge tone={row.status === "active" ? "success" : row.status === "invited" ? "warning" : "critical"}>{titleCase(row.status)}</Badge>,
    },
    {
      key: "lastActiveAt",
      header: "Last active",
      render: (row) => (row.lastActiveAt ? formatRelativeTime(row.lastActiveAt) : "—"),
    },
  ];

  return (
    <Card>
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <h3 className="text-sm font-semibold text-ink">Users</h3>
        <Button
          size="sm"
          variant="primary"
          className="gap-1.5"
          disabled={!canInvite}
          title={canInvite ? undefined : "Requires the tenancy:manage permission"}
          onClick={() => setIsInviteOpen(true)}
        >
          <UserPlus className="h-3.5 w-3.5" />
          Invite user
        </Button>
      </div>
      <DataTable
        columns={columns}
        rows={usersQuery.data ?? []}
        rowKey={(row) => row.id}
        isLoading={usersQuery.isLoading}
        isError={usersQuery.isError}
        onRetry={() => usersQuery.refetch()}
      />
      {canInvite && <InviteUserModal open={isInviteOpen} onClose={() => setIsInviteOpen(false)} />}
    </Card>
  );
}
