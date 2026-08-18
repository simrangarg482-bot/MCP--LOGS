import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, UserPlus } from "lucide-react";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Modal } from "@/components/ui/Modal";
import { Input } from "@/components/ui/Input";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { DataTable, type DataTableColumn } from "@/components/data/DataTable";
import { createInvitation, listInvitations, listOrgUsers, revokeInvitation } from "@/api/tenancy";
import type { Invitation, OrgUser } from "@/types/tenancy";
import { useTenant } from "@/context/TenantContext";
import { useToast } from "@/context/ToastContext";
import { useAuth } from "@/context/AuthContext";
import { formatRelativeTime } from "@/utils/date";
import { titleCase } from "@/utils/format";

const INVITATION_STATUS_TONE = { pending: "warning", accepted: "success", expired: "neutral", revoked: "critical" } as const;

const ROLE_TONE = { owner: "accent", admin: "info", member: "neutral", viewer: "neutral" } as const;

/** No email-sending exists in this codebase (docs/USER_TESTING_GUIDE.md
 * section 3), so this is the only place the accept link is ever surfaced --
 * copy it out and deliver it to the invitee some other way (Slack, a manual
 * email, etc). `invitation.token` is only ever present on this one response
 * (see `Invitation.token`'s own docstring), so there is no "view the link
 * again later" -- if it's lost, revoke and re-invite instead.
 */
function InvitationLinkPanel({ invitation, onDone }: { invitation: Invitation; onDone: () => void }) {
  const { toast } = useToast();
  const acceptUrl = `${window.location.origin}/invitations/${invitation.id}/accept?token=${encodeURIComponent(invitation.token ?? "")}`;

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(acceptUrl);
      toast({ variant: "success", title: "Link copied" });
    } catch {
      toast({ variant: "error", title: "Couldn't copy link", description: "Copy it manually instead." });
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-ink-muted">
        Invitation sent to <span className="font-medium text-ink">{invitation.email}</span>. Since EKIP doesn't send
        invitation emails yet, share this link with them directly -- it won't be shown again.
      </p>
      <div className="flex gap-2">
        <Input readOnly value={acceptUrl} onFocus={(event) => event.target.select()} />
        <Button type="button" variant="secondary" size="sm" className="shrink-0 gap-1.5" onClick={handleCopy}>
          <Copy className="h-3.5 w-3.5" />
          Copy
        </Button>
      </div>
      <div className="mt-2 flex justify-end">
        <Button type="button" variant="primary" size="sm" onClick={onDone}>
          Done
        </Button>
      </div>
    </div>
  );
}

function InviteUserModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { organization } = useTenant();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  // Free text, not a fixed dropdown: `core.users.service.ensure_admin_role`
  // is the only role any deployment of this codebase actually creates, and
  // `create_invitation` 422s with `role.not_found` for anything else --
  // defaulting to "admin" reflects that reality rather than offering
  // "member"/"viewer" options that always fail. Same pattern as
  // `AccessRulesSettingsPage`'s `grantsRole` field for the identical
  // constraint.
  const [role, setRole] = useState("admin");
  const [createdInvitation, setCreatedInvitation] = useState<Invitation | null>(null);

  function handleClose() {
    setEmail("");
    setRole("admin");
    setCreatedInvitation(null);
    onClose();
  }

  const inviteMutation = useMutation({
    mutationFn: () => {
      if (!organization) throw new Error("No organization selected");
      return createInvitation(organization.id, { email, grantsRole: role });
    },
    onSuccess: (invitation) => {
      toast({ variant: "success", title: `Invitation created for ${email}` });
      queryClient.invalidateQueries({ queryKey: ["users"] });
      queryClient.invalidateQueries({ queryKey: ["invitations"] });
      setCreatedInvitation(invitation);
    },
    onError: () => {
      toast({ variant: "error", title: "Failed to send invitation" });
    },
  });

  if (createdInvitation) {
    return (
      <Modal open={open} onClose={handleClose} title="Invitation created">
        <InvitationLinkPanel invitation={createdInvitation} onDone={handleClose} />
      </Modal>
    );
  }

  return (
    <Modal open={open} onClose={handleClose} title="Invite a user" description="They'll receive access once they accept.">
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
          <Input required placeholder="admin" value={role} onChange={(event) => setRole(event.target.value)} />
          <span className="text-xs font-normal text-ink-subtle">
            Exact role name as configured for your organization. "admin" is the only role EKIP creates automatically
            today.
          </span>
        </label>
        <div className="mt-2 flex justify-end gap-2">
          <Button type="button" variant="secondary" size="sm" onClick={handleClose}>
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

function PendingInvitationsCard({ canManage }: { canManage: boolean }) {
  const { organization } = useTenant();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [revokeTarget, setRevokeTarget] = useState<Invitation | null>(null);

  // `list_invitations`/`revoke_invitation` both require `tenancy:manage`
  // (`core.tenancy.service`) -- same gate as inviting in the first place.
  const invitationsQuery = useQuery({
    queryKey: ["invitations", organization?.id],
    queryFn: () => listInvitations(organization!.id),
    enabled: Boolean(organization) && canManage,
  });

  const revokeMutation = useMutation({
    mutationFn: (invitationId: string) => revokeInvitation(invitationId),
    onSuccess: () => {
      toast({ variant: "success", title: "Invitation revoked" });
      queryClient.invalidateQueries({ queryKey: ["invitations"] });
      setRevokeTarget(null);
    },
    onError: () => {
      toast({ variant: "error", title: "Failed to revoke invitation" });
      setRevokeTarget(null);
    },
  });

  if (!canManage) return null;

  const columns: DataTableColumn<Invitation>[] = [
    { key: "email", header: "Email", render: (row) => <span className="text-ink">{row.email}</span> },
    {
      key: "status",
      header: "Status",
      render: (row) => <Badge tone={INVITATION_STATUS_TONE[row.status]}>{titleCase(row.status)}</Badge>,
    },
    {
      key: "expiresAt",
      header: "Expires",
      render: (row) => formatRelativeTime(row.expiresAt),
    },
    {
      key: "actions",
      header: "",
      render: (row) =>
        row.status === "pending" ? (
          <Button size="sm" variant="secondary" onClick={() => setRevokeTarget(row)}>
            Revoke
          </Button>
        ) : (
          <span className="text-xs text-ink-subtle">No further actions</span>
        ),
    },
  ];

  return (
    <Card>
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <h3 className="text-sm font-semibold text-ink">Pending invitations</h3>
      </div>
      <DataTable
        columns={columns}
        rows={invitationsQuery.data ?? []}
        rowKey={(row) => row.id}
        isLoading={invitationsQuery.isLoading}
        isError={invitationsQuery.isError}
        onRetry={() => invitationsQuery.refetch()}
        emptyTitle="No invitations sent yet"
      />
      <ConfirmDialog
        open={revokeTarget !== null}
        title="Revoke this invitation?"
        description={
          revokeTarget
            ? `"${revokeTarget.email}" will no longer be able to accept this invitation. This cannot be undone -- you'd need to send a new invitation.`
            : undefined
        }
        confirmLabel="Revoke"
        destructive
        isLoading={revokeMutation.isPending}
        onConfirm={() => revokeTarget && revokeMutation.mutate(revokeTarget.id)}
        onCancel={() => setRevokeTarget(null)}
      />
    </Card>
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
    <div className="flex flex-col gap-4">
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
      <PendingInvitationsCard canManage={canInvite} />
    </div>
  );
}
