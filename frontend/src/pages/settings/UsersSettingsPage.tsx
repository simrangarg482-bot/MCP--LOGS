import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Copy, Link2, Loader2, Mail, UserPlus } from "lucide-react";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Modal } from "@/components/ui/Modal";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { DataTable, type DataTableColumn } from "@/components/data/DataTable";
import { createInvitation, listInvitations, listOrgUsers, resendInvitation, revokeInvitation } from "@/api/tenancy";
import { listPermissions, listRoles, setMemberRoles } from "@/api/roles";
import { deactivateMember, reactivateMember } from "@/api/scim";
import { scimErrorMessage } from "@/utils/scim";
import { RolePermissionSummary } from "@/components/domain/RolePermissionSummary";
import type { Invitation, OrgUser } from "@/types/tenancy";
import type { Role } from "@/types/roles";
import { canGrantRole, roleErrorMessage, unionPermissions } from "@/utils/roles";
import { emailStatusView, invitationErrorMessage, manualInvitationLink } from "@/utils/invitations";
import { useTenant } from "@/context/TenantContext";
import { useToast } from "@/context/ToastContext";
import { useAuth } from "@/context/AuthContext";
import { formatRelativeTime } from "@/utils/date";
import { titleCase } from "@/utils/format";

const INVITATION_STATUS_TONE = { pending: "warning", accepted: "success", expired: "neutral", revoked: "critical" } as const;

const roleTone = (name: string) => (name === "admin" ? "info" : "accent");

/** The copy-link fallback: used when the server has no email provider, when an
 * email failed, and for "Copy new link". `invitation.token` is only ever present
 * on the one response that created it (see `Invitation.token`'s own docstring),
 * so the link can't be shown again later -- generate a new one if it is lost
 * (which also invalidates this one).
 */
function InvitationLinkPanel({
  invitation,
  intro,
  onDone,
}: {
  invitation: Invitation;
  intro: string;
  onDone: () => void;
}) {
  const { toast } = useToast();
  const acceptUrl = manualInvitationLink(invitation.id, invitation.token ?? "");

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(acceptUrl);
      toast({ variant: "success", title: "Link copied" });
    } catch {
      toast({ variant: "error", title: "Couldn't copy link", description: "Copy it manually instead." });
    }
  }

  return (
    <div className="flex flex-col gap-3" data-testid="invitation-link-panel">
      <p className="text-sm text-ink-muted">{intro}</p>
      <div className="flex gap-2">
        <Input readOnly value={acceptUrl} aria-label="Invitation link" onFocus={(event) => event.target.select()} />
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

/** Live delivery status right after inviting: the mail is sent in the background,
 * so this polls the invitation until it leaves `queued`. "Sent" only ever means
 * the mail server accepted the message. */
function InvitationEmailStatusPanel({
  invitationId,
  email,
  onDone,
  onLinkCreated,
}: {
  invitationId: string;
  email: string;
  onDone: () => void;
  onLinkCreated: (invitation: Invitation) => void;
}) {
  const { organization } = useTenant();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const listQuery = useQuery({
    queryKey: ["invitations", organization?.id],
    queryFn: () => listInvitations(organization!.id),
    enabled: Boolean(organization),
    refetchInterval: (query) =>
      query.state.data?.find((i) => i.id === invitationId)?.emailStatus === "queued" || !query.state.data ? 2000 : false,
  });
  const invitation = listQuery.data?.find((i) => i.id === invitationId);
  const status = invitation?.emailStatus ?? "queued";

  const copyLink = useMutation({
    mutationFn: () => resendInvitation(organization!.id, invitationId, "link"),
    onSuccess: (fresh) => {
      queryClient.invalidateQueries({ queryKey: ["invitations"] });
      onLinkCreated(fresh);
    },
    onError: (error) => toast({ variant: "error", title: "Couldn't create a link", description: invitationErrorMessage(error) }),
  });

  return (
    <div className="flex flex-col gap-3" data-testid="invitation-email-status" data-status={status}>
      {status === "queued" && (
        <p className="flex items-center gap-2 text-sm text-ink-muted" role="status">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          Sending the invitation email to <span className="font-medium text-ink">{email}</span>…
        </p>
      )}
      {status === "sent" && (
        <p className="flex items-start gap-2 text-sm text-ink-muted" role="status">
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" aria-hidden="true" />
          <span>
            The invitation email was accepted for delivery to <span className="font-medium text-ink">{email}</span>. It
            contains a link that is valid for 14 days. After accepting, they confirm their address through a second email.
          </span>
        </p>
      )}
      {(status === "failed" || status === "not_configured" || status === "not_sent") && (
        <p role="alert" className="rounded-lg border border-critical-border bg-critical-subtle px-3 py-2 text-sm text-critical">
          The invitation was created, but the email could not be delivered. You can copy a link and send it to{" "}
          {email} yourself, or try sending the email again from the pending invitations list.
        </p>
      )}
      <div className="mt-2 flex justify-end gap-2">
        {(status === "failed" || status === "not_configured" || status === "not_sent") && (
          <Button
            type="button"
            variant="secondary"
            size="sm"
            className="gap-1.5"
            isLoading={copyLink.isPending}
            onClick={() => copyLink.mutate()}
          >
            <Link2 className="h-3.5 w-3.5" />
            Copy new link
          </Button>
        )}
        <Button type="button" variant="primary" size="sm" onClick={onDone}>
          Done
        </Button>
      </div>
    </div>
  );
}

function InviteUserModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { organization } = useTenant();
  const { user } = useAuth();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  const [roleId, setRoleId] = useState("");
  const [createdInvitation, setCreatedInvitation] = useState<Invitation | null>(null);
  const [copiedLink, setCopiedLink] = useState<Invitation | null>(null);
  const myPermissions = user?.permissions ?? [];

  // The roles an invitation can grant: built-in + this organization's own,
  // straight from the backend (`GET /organizations/{id}/roles`).
  const rolesQuery = useQuery({
    queryKey: ["roles", organization?.id],
    queryFn: () => listRoles(organization!.id),
    enabled: Boolean(organization) && open,
  });
  const catalogQuery = useQuery({
    queryKey: ["permissions", organization?.id],
    queryFn: () => listPermissions(organization!.id),
    enabled: Boolean(organization) && open,
    staleTime: 5 * 60 * 1000,
  });
  const roles = rolesQuery.data ?? [];
  // Default to the least-privileged grantable role, never silently to admin.
  const defaultRole = [...roles]
    .filter((role) => canGrantRole(role, myPermissions))
    .sort((a, b) => a.permissions.length - b.permissions.length)[0];
  const selectedRole: Role | undefined = roles.find((role) => role.id === roleId) ?? defaultRole;

  function handleClose() {
    setEmail("");
    setRoleId("");
    setCreatedInvitation(null);
    setCopiedLink(null);
    onClose();
  }

  const inviteMutation = useMutation({
    mutationFn: () => {
      if (!organization) throw new Error("No organization selected");
      if (!selectedRole) throw new Error("Choose a role");
      return createInvitation(organization.id, { email, grantsRole: selectedRole.name });
    },
    onSuccess: (invitation) => {
      toast({ variant: "success", title: `Invitation created for ${email}` });
      queryClient.invalidateQueries({ queryKey: ["users"] });
      queryClient.invalidateQueries({ queryKey: ["invitations"] });
      queryClient.invalidateQueries({ queryKey: ["roles"] });
      setCreatedInvitation(invitation);
    },
    onError: (error) => {
      toast({ variant: "error", title: "Failed to send invitation", description: invitationErrorMessage(error) });
    },
  });

  if (createdInvitation) {
    // `token` is present only when the server could not email (no provider):
    // the original copy-link workflow. Otherwise the mail is going out.
    const manualLink = copiedLink ?? (createdInvitation.token ? createdInvitation : null);
    return (
      <Modal open={open} onClose={handleClose} title={manualLink ? "Share this invitation link" : "Invitation created"}>
        {manualLink ? (
          <InvitationLinkPanel
            invitation={manualLink}
            intro={
              createdInvitation.emailStatus === "not_configured"
                ? `Invitation created for ${createdInvitation.email}. Email delivery isn't configured on this server, so share this link with them directly - it won't be shown again.`
                : `New link for ${createdInvitation.email}. Any earlier link no longer works. It won't be shown again.`
            }
            onDone={handleClose}
          />
        ) : (
          <InvitationEmailStatusPanel
            invitationId={createdInvitation.id}
            email={createdInvitation.email}
            onDone={handleClose}
            onLinkCreated={setCopiedLink}
          />
        )}
      </Modal>
    );
  }

  return (
    <Modal open={open} onClose={handleClose} title="Invite a user" description="They'll receive access once they accept.">
      <form
        className="flex flex-col gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          if (inviteMutation.isPending) return; // no duplicate submissions
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
          <Select
            aria-label="Role"
            value={selectedRole?.id ?? ""}
            disabled={rolesQuery.isLoading || roles.length === 0}
            onChange={(event) => setRoleId(event.target.value)}
          >
            {roles.map((role) => (
              <option key={role.id} value={role.id} disabled={!canGrantRole(role, myPermissions)}>
                {role.name}
                {role.isBuiltin ? " (built-in)" : ""}
                {canGrantRole(role, myPermissions) ? "" : " - needs permissions you don't have"}
              </option>
            ))}
          </Select>
          {rolesQuery.isError && (
            <span className="text-xs font-normal text-critical">Could not load roles. Close and try again.</span>
          )}
        </label>
        {selectedRole && (
          <>
            {selectedRole.description && <p className="text-xs text-ink-muted">{selectedRole.description}</p>}
            <RolePermissionSummary
              heading={`"${selectedRole.name}" can:`}
              codes={selectedRole.permissions}
              catalog={catalogQuery.data ?? []}
              emptyText="This role grants no permissions."
            />
          </>
        )}
        <div className="mt-2 flex justify-end gap-2">
          <Button type="button" variant="secondary" size="sm" onClick={handleClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" size="sm" disabled={inviteMutation.isPending || !email || !selectedRole}>
            {inviteMutation.isPending ? "Sending..." : "Send invitation"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

/** Add, remove or change the roles of an existing member. */
function MemberRolesModal({ member, onClose }: { member: OrgUser | null; onClose: () => void }) {
  const { organization } = useTenant();
  const { user, refreshUser } = useAuth();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const myPermissions = user?.permissions ?? [];

  const rolesQuery = useQuery({
    queryKey: ["roles", organization?.id],
    queryFn: () => listRoles(organization!.id),
    enabled: Boolean(organization) && member !== null,
  });
  const catalogQuery = useQuery({
    queryKey: ["permissions", organization?.id],
    queryFn: () => listPermissions(organization!.id),
    enabled: Boolean(organization) && member !== null,
    staleTime: 5 * 60 * 1000,
  });

  useEffect(() => {
    setSelected(member?.roleIds ?? []);
    setError(null);
  }, [member]);

  const mutation = useMutation({
    mutationFn: () => setMemberRoles(organization!.id, member!.id, selected),
    onSuccess: async () => {
      toast({ variant: "success", title: `Roles updated for ${member?.name}` });
      await queryClient.invalidateQueries({ queryKey: ["users"] });
      await queryClient.invalidateQueries({ queryKey: ["roles"] });
      if (member?.id === user?.id) await refreshUser().catch(() => undefined);
      onClose();
    },
    onError: (err) => setError(roleErrorMessage(err)),
  });

  if (!member) return null;
  const roles = rolesQuery.data ?? [];
  const held = new Set(member.roleIds);
  const unchanged = selected.length === held.size && selected.every((id) => held.has(id));

  function toggle(roleId: string) {
    setSelected((current) => (current.includes(roleId) ? current.filter((id) => id !== roleId) : [...current, roleId]));
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={`Roles for ${member.name}`}
      description="Members can hold several roles; their access is the combination of all of them."
    >
      <form
        className="flex flex-col gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          mutation.mutate();
        }}
      >
        {rolesQuery.isLoading && <p className="text-xs text-ink-muted">Loading roles...</p>}
        {rolesQuery.isError && <p className="text-xs text-critical">Could not load roles.</p>}
        <div className="flex flex-col gap-1.5" role="group" aria-label="Roles">
          {roles.map((role) => {
            const checked = selected.includes(role.id);
            // Can't hand out a role stronger than your own access -- but a role
            // the member already holds can always be taken away.
            const blocked = !checked && !canGrantRole(role, myPermissions);
            return (
              <label
                key={role.id}
                className="flex cursor-pointer items-start gap-2.5 rounded-lg border border-white/10 px-3 py-2 hover:bg-white/[0.04]"
                title={blocked ? "You can only grant roles whose permissions you hold yourself" : undefined}
              >
                <input
                  type="checkbox"
                  className="mt-0.5 h-4 w-4 shrink-0"
                  checked={checked}
                  disabled={blocked}
                  onChange={() => toggle(role.id)}
                  aria-label={role.name}
                />
                <span className="min-w-0">
                  <span className="flex items-center gap-2 text-sm font-medium text-ink">
                    {role.name}
                    {role.isBuiltin && <Badge tone="info">Built-in</Badge>}
                  </span>
                  {role.description && <span className="block text-xs text-ink-muted">{role.description}</span>}
                </span>
              </label>
            );
          })}
        </div>
        <RolePermissionSummary
          heading="Resulting permissions"
          codes={unionPermissions(roles, selected)}
          catalog={catalogQuery.data ?? []}
          emptyText="Select at least one role."
        />
        {error && (
          <p role="alert" className="rounded-lg border border-critical-border bg-critical-subtle px-3 py-2 text-xs text-critical">
            {error}
          </p>
        )}
        <div className="mt-1 flex justify-end gap-2">
          <Button type="button" variant="secondary" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="submit"
            variant="primary"
            size="sm"
            isLoading={mutation.isPending}
            disabled={selected.length === 0 || unchanged}
          >
            Save roles
          </Button>
        </div>
      </form>
    </Modal>
  );
}

/**
 * Deactivate or reactivate one member's access to THIS organization (never their account elsewhere, and nothing
 * of the organization is deleted). A reason is required; deactivation asks for an explicit confirmation
 * because the person is signed out immediately.
 */
function MemberAccessModal({
  target,
  onClose,
}: {
  target: { member: OrgUser; action: "deactivate" | "reactivate" } | null;
  onClose: () => void;
}) {
  const { organization } = useTenant();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const [understood, setUnderstood] = useState(false);
  const [attempted, setAttempted] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);

  function handleClose() {
    setReason("");
    setUnderstood(false);
    setAttempted(false);
    setServerError(null);
    onClose();
  }

  const mutation = useMutation({
    mutationFn: () => {
      const fn = target!.action === "deactivate" ? deactivateMember : reactivateMember;
      return fn(organization!.id, target!.member.id, reason.trim());
    },
    onSuccess: () => {
      toast({
        variant: "success",
        title: target!.action === "deactivate" ? "Access deactivated" : "Access reactivated",
        description:
          target!.action === "deactivate"
            ? `${target!.member.name} was signed out of this organization.`
            : `${target!.member.name} needs to sign in again.`,
      });
      void queryClient.invalidateQueries({ queryKey: ["users"] });
      void queryClient.invalidateQueries({ queryKey: ["scim"] });
      handleClose();
    },
    onError: (error) => setServerError(scimErrorMessage(error)),
  });

  if (!target) return null;
  const deactivating = target.action === "deactivate";
  const reasonError = reason.trim() ? null : "A reason is required.";
  const confirmError = deactivating && !understood ? "Confirm that you understand the effect." : null;

  return (
    <Modal
      open
      onClose={handleClose}
      title={deactivating ? `Deactivate ${target.member.name}?` : `Reactivate ${target.member.name}?`}
      description={target.member.email}
    >
      <form
        noValidate
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          setAttempted(true);
          setServerError(null);
          if (reasonError || confirmError) return;
          mutation.mutate();
        }}
      >
        <p className="text-xs text-ink-muted">
          {deactivating
            ? "They are signed out of this organization immediately and cannot sign in, use the API or the MCP tools until you reactivate them. Their account in other organizations is not affected, and nothing of this organization is deleted."
            : "They get access back, but every session from before the deactivation stays ended: they must sign in again."}
        </p>
        <div>
          <label htmlFor="member-access-reason" className="mb-1.5 block text-xs font-medium text-ink-muted">
            Reason (required, recorded in the audit log)
          </label>
          <Input id="member-access-reason" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={1000} autoFocus />
          {attempted && reasonError && (
            <p role="alert" className="mt-1 text-xs text-critical">
              {reasonError}
            </p>
          )}
        </div>
        {deactivating && (
          <label className="flex items-start gap-2 text-xs text-ink">
            <input type="checkbox" className="mt-0.5" checked={understood} onChange={(e) => setUnderstood(e.target.checked)} />
            <span>
              I understand that <strong>{target.member.name}</strong> will be signed out immediately.
            </span>
          </label>
        )}
        {attempted && confirmError && (
          <p role="alert" className="-mt-2 text-xs text-critical">
            {confirmError}
          </p>
        )}
        {serverError && (
          <p role="alert" className="text-xs text-critical" data-testid="member-access-error">
            {serverError}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" size="sm" variant="secondary" onClick={handleClose}>
            Cancel
          </Button>
          <Button type="submit" size="sm" variant={deactivating ? "danger" : "primary"} isLoading={mutation.isPending}>
            {deactivating ? "Deactivate access" : "Reactivate access"}
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
  const [newLink, setNewLink] = useState<Invitation | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  // `list_invitations`/`revoke_invitation` both require `tenancy:manage`
  // (`core.tenancy.service`) -- same gate as inviting in the first place.
  // While any mail is still being sent, keep refreshing so its status updates.
  const invitationsQuery = useQuery({
    queryKey: ["invitations", organization?.id],
    queryFn: () => listInvitations(organization!.id),
    enabled: Boolean(organization) && canManage,
    refetchInterval: (query) => (query.state.data?.some((i) => i.emailStatus === "queued") ? 3000 : false),
  });

  const resendMutation = useMutation({
    mutationFn: ({ id, method }: { id: string; method: "email" | "link" }) =>
      resendInvitation(organization!.id, id, method),
    onMutate: ({ id }) => setBusyId(id),
    onSuccess: (invitation, { method }) => {
      queryClient.invalidateQueries({ queryKey: ["invitations"] });
      if (method === "link") {
        setNewLink(invitation);
      } else {
        toast({ variant: "success", title: "Invitation email is being sent", description: "The previous link no longer works." });
      }
    },
    onError: (error) => toast({ variant: "error", title: "Couldn't update the invitation", description: invitationErrorMessage(error) }),
    onSettled: () => setBusyId(null),
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
      key: "email",
      header: "Email",
      render: (row) => {
        if (row.status !== "pending") return <span className="text-xs text-ink-subtle">-</span>;
        const view = emailStatusView(row.emailStatus);
        return (
          <div className="flex flex-col gap-0.5">
            <Badge tone={view.tone} data-testid="email-status" data-status={row.emailStatus}>
              {view.label}
            </Badge>
            {row.emailStatus === "sent" && row.emailSentAt && (
              <span className="text-xs text-ink-subtle">{formatRelativeTime(row.emailSentAt)}</span>
            )}
          </div>
        );
      },
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
          <div className="flex flex-wrap justify-end gap-2">
            <Button
              size="sm"
              variant="secondary"
              className="gap-1.5"
              disabled={busyId !== null || row.emailStatus === "queued"}
              isLoading={busyId === row.id && resendMutation.variables?.method === "email"}
              onClick={() => resendMutation.mutate({ id: row.id, method: "email" })}
            >
              <Mail className="h-3.5 w-3.5" />
              Resend
            </Button>
            <Button
              size="sm"
              variant="secondary"
              className="gap-1.5"
              disabled={busyId !== null}
              isLoading={busyId === row.id && resendMutation.variables?.method === "link"}
              onClick={() => resendMutation.mutate({ id: row.id, method: "link" })}
            >
              <Link2 className="h-3.5 w-3.5" />
              Copy new link
            </Button>
            <Button size="sm" variant="secondary" disabled={busyId !== null} onClick={() => setRevokeTarget(row)}>
              Revoke
            </Button>
          </div>
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
      <Modal open={newLink !== null} onClose={() => setNewLink(null)} title="New invitation link">
        {newLink && (
          <InvitationLinkPanel
            invitation={newLink}
            intro={`New link for ${newLink.email}. The previous link no longer works, and this one won't be shown again.`}
            onDone={() => setNewLink(null)}
          />
        )}
      </Modal>
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
  const [rolesTarget, setRolesTarget] = useState<OrgUser | null>(null);
  const [accessTarget, setAccessTarget] = useState<{ member: OrgUser; action: "deactivate" | "reactivate" } | null>(null);

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
    {
      key: "roles",
      header: "Roles",
      render: (row) => (
        <div className="flex flex-wrap gap-1.5">
          {row.roles.length === 0 ? (
            <span className="text-xs text-ink-subtle">-</span>
          ) : (
            row.roles.map((name) => (
              <Badge key={name} tone={roleTone(name)}>
                {name}
              </Badge>
            ))
          )}
        </div>
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (row) =>
        row.membershipStatus === "deactivated" ? (
          <div className="flex flex-col gap-0.5" data-testid={`member-status-${row.id}`} data-status="deactivated">
            <Badge tone="critical">Deactivated</Badge>
            <span className="text-[11px] text-ink-subtle">
              {row.membershipSource === "scim" ? "By your identity provider" : "By an administrator"}
            </span>
          </div>
        ) : (
          <span data-testid={`member-status-${row.id}`} data-status="active">
            <Badge tone={row.status === "active" ? "success" : row.status === "invited" ? "warning" : "critical"}>{titleCase(row.status)}</Badge>
          </span>
        ),
    },
    {
      key: "lastActiveAt",
      header: "Last active",
      render: (row) => (row.lastActiveAt ? formatRelativeTime(row.lastActiveAt) : "—"),
    },
    {
      key: "actions",
      header: "",
      render: (row) =>
        canInvite ? (
          <div className="flex flex-wrap justify-end gap-2">
            <Button size="sm" variant="secondary" onClick={() => setRolesTarget(row)}>
              Manage roles
            </Button>
            {row.membershipStatus === "deactivated" ? (
              <Button size="sm" variant="secondary" onClick={() => setAccessTarget({ member: row, action: "reactivate" })}>
                Reactivate
              </Button>
            ) : (
              <Button size="sm" variant="danger" onClick={() => setAccessTarget({ member: row, action: "deactivate" })}>
                Deactivate
              </Button>
            )}
          </div>
        ) : null,
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
        {canInvite && <MemberRolesModal member={rolesTarget} onClose={() => setRolesTarget(null)} />}
        {canInvite && <MemberAccessModal target={accessTarget} onClose={() => setAccessTarget(null)} />}
      </Card>
      <PendingInvitationsCard canManage={canInvite} />
    </div>
  );
}
