import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Copy, KeyRound, RefreshCw } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Modal } from "@/components/ui/Modal";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorState } from "@/components/ui/ErrorState";
import { LoadingState } from "@/components/ui/LoadingState";
import {
  createScimToken,
  decideScimPending,
  getScimOverview,
  linkScimIdentity,
  listScimGroups,
  listScimIdentities,
  listScimPending,
  listScimTokens,
  revokeScimToken,
  rotateScimToken,
  setScimGroupRoles,
  updateScimConfig,
} from "@/api/scim";
import { listOrgUsers } from "@/api/tenancy";
import { listRoles } from "@/api/roles";
import { useAuth } from "@/context/AuthContext";
import { useTenant } from "@/context/TenantContext";
import { useToast } from "@/context/ToastContext";
import { formatRelativeTime } from "@/utils/date";
import { scimErrorMessage, syncHealth, tokenStatusTone, tokenWarnings, type SyncLevel } from "@/utils/scim";
import type { ApiError } from "@/types/common";
import type { ScimGroup, ScimIdentity, ScimOverview, ScimPendingAction, ScimToken, ScimTokenCreated } from "@/types/scim";

const HEALTH_TONE: Record<SyncLevel, "neutral" | "success" | "warning" | "critical"> = {
  off: "neutral",
  no_token: "warning",
  never: "neutral",
  ok: "success",
  stale: "warning",
  error: "critical",
};

function Banner({ tone, children, testId }: { tone: "warning" | "critical" | "info"; children: React.ReactNode; testId?: string }) {
  const classes =
    tone === "critical"
      ? "border-critical-border bg-critical-subtle text-critical"
      : tone === "warning"
        ? "border-warning-border bg-warning-subtle text-warning"
        : "border-white/10 bg-white/[0.04] text-ink-muted";
  return (
    <div role={tone === "info" ? "status" : "alert"} data-testid={testId} className={`flex items-start gap-2 rounded-md border px-3 py-2 text-xs ${classes}`}>
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <div>{children}</div>
    </div>
  );
}

function CopyField({ label, value, id }: { label: string; value: string; id: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // clipboard unavailable: the value is selectable in the field
    }
  }
  return (
    <div>
      <label htmlFor={id} className="mb-1.5 block text-xs font-medium text-ink-muted">
        {label}
      </label>
      <div className="flex gap-2">
        <Input id={id} readOnly value={value} className="font-mono text-xs" onFocus={(e) => e.currentTarget.select()} />
        <Button type="button" size="sm" variant="secondary" className="shrink-0 gap-1.5" onClick={copy} aria-label={`Copy ${label}`}>
          {copied ? <CheckCircle2 className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------- one-time token

function TokenSecretModal({ created, onClose }: { created: ScimTokenCreated | null; onClose: () => void }) {
  if (!created) return null;
  return (
    <Modal open onClose={onClose} title="Copy your SCIM token" description="This is the only time it is shown.">
      <div className="flex flex-col gap-4" data-testid="scim-token-secret">
        <Banner tone="warning">
          Store this token in your identity provider now. EKIP keeps only a hash, so it cannot be shown again. If you lose it,
          create a new one.
        </Banner>
        <CopyField label="SCIM token" value={created.rawToken} id="scim-raw-token" />
        <p className="text-xs text-ink-muted">
          Name: {created.token.name} · expires {new Date(created.token.expiresAt).toLocaleDateString()}
        </p>
        <div className="flex justify-end">
          <Button variant="primary" size="sm" onClick={onClose}>
            I have stored it
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function CreateTokenModal({
  open,
  onClose,
  onCreated,
  organizationId,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (created: ScimTokenCreated) => void;
  organizationId: string;
}) {
  const { toast } = useToast();
  const [name, setName] = useState("");
  const [ttl, setTtl] = useState("90");
  const mutation = useMutation({
    mutationFn: () => createScimToken(organizationId, name.trim(), Number(ttl)),
    onSuccess: (created) => {
      setName("");
      onCreated(created);
    },
    onError: (error) => toast({ variant: "error", title: "Couldn't create the token", description: scimErrorMessage(error) }),
  });
  return (
    <Modal open={open} onClose={onClose} title="Create a SCIM token" description="Give it to your identity provider as the secret token.">
      <form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (name.trim()) mutation.mutate();
        }}
      >
        <div>
          <label htmlFor="scim-token-name" className="mb-1.5 block text-xs font-medium text-ink-muted">
            Name
          </label>
          <Input id="scim-token-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Entra ID production" maxLength={100} autoFocus />
        </div>
        <div>
          <label htmlFor="scim-token-ttl" className="mb-1.5 block text-xs font-medium text-ink-muted">
            Expires after
          </label>
          <Select id="scim-token-ttl" value={ttl} onChange={(e) => setTtl(e.target.value)}>
            <option value="30">30 days</option>
            <option value="90">90 days</option>
            <option value="180">180 days</option>
            <option value="365">365 days</option>
          </Select>
        </div>
        <div className="flex justify-end gap-2">
          <Button type="button" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" size="sm" variant="primary" isLoading={mutation.isPending} disabled={!name.trim()}>
            Create token
          </Button>
        </div>
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------------------------------------- sections

function TokensCard({ organizationId, overview }: { organizationId: string; overview: ScimOverview }) {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [secret, setSecret] = useState<ScimTokenCreated | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<ScimToken | null>(null);
  const [rotateTarget, setRotateTarget] = useState<ScimToken | null>(null);

  const tokensQuery = useQuery({ queryKey: ["scim", "tokens", organizationId], queryFn: () => listScimTokens(organizationId) });
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["scim"] });
  };

  const revoke = useMutation({
    mutationFn: (id: string) => revokeScimToken(organizationId, id),
    onSuccess: () => {
      toast({ variant: "success", title: "Token revoked", description: "It stops working immediately." });
      setRevokeTarget(null);
      refresh();
    },
    onError: (error) => {
      toast({ variant: "error", title: "Couldn't revoke the token", description: scimErrorMessage(error) });
      setRevokeTarget(null);
    },
  });
  const rotate = useMutation({
    mutationFn: (id: string) => rotateScimToken(organizationId, id),
    onSuccess: (created) => {
      setRotateTarget(null);
      setSecret(created);
      refresh();
    },
    onError: (error) => {
      toast({ variant: "error", title: "Couldn't rotate the token", description: scimErrorMessage(error) });
      setRotateTarget(null);
    },
  });

  const tokens = tokensQuery.data ?? [];
  const warnings = tokenWarnings(tokens);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <KeyRound className="h-4 w-4" aria-hidden="true" />
          Tokens
        </CardTitle>
        <Button size="sm" variant="primary" onClick={() => setCreating(true)}>
          Create token
        </Button>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {warnings.map((w) => (
          <Banner key={w} tone="warning" testId="token-warning">
            {w}
          </Banner>
        ))}
        {tokensQuery.isLoading && <LoadingState label="Loading tokens…" />}
        {tokensQuery.isError && <ErrorState title="Could not load tokens" onRetry={() => void tokensQuery.refetch()} />}
        {tokensQuery.data && tokens.length === 0 && (
          <EmptyState title="No tokens yet" description="Create one and give it to your identity provider's SCIM connection." />
        )}
        {tokens.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs" data-testid="scim-tokens">
              <thead className="text-ink-subtle">
                <tr>
                  <th className="py-2 pr-3 font-medium">Name</th>
                  <th className="py-2 pr-3 font-medium">Token</th>
                  <th className="py-2 pr-3 font-medium">Status</th>
                  <th className="py-2 pr-3 font-medium">Expires</th>
                  <th className="py-2 pr-3 font-medium">Last used</th>
                  <th className="py-2" />
                </tr>
              </thead>
              <tbody>
                {tokens.map((t) => (
                  <tr key={t.id} className="border-t border-white/10 align-middle" data-testid={`scim-token-${t.id}`}>
                    <td className="py-2 pr-3 text-ink">{t.name}</td>
                    <td className="py-2 pr-3 font-mono text-ink-muted">{t.prefix}…</td>
                    <td className="py-2 pr-3">
                      <Badge tone={tokenStatusTone(t.status)}>{t.status}</Badge>
                    </td>
                    <td className="py-2 pr-3 text-ink-muted">{new Date(t.expiresAt).toLocaleDateString()}</td>
                    <td className="py-2 pr-3 text-ink-muted">{t.lastUsedAt ? formatRelativeTime(t.lastUsedAt) : "never"}</td>
                    <td className="py-2 text-right">
                      {t.status === "active" && (
                        <div className="flex justify-end gap-2">
                          <Button size="sm" className="gap-1.5" onClick={() => setRotateTarget(t)}>
                            <RefreshCw className="h-3 w-3" aria-hidden="true" />
                            Rotate
                          </Button>
                          <Button size="sm" variant="danger" onClick={() => setRevokeTarget(t)}>
                            Revoke
                          </Button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="text-xs text-ink-subtle">
          {overview.activeTokens} active token{overview.activeTokens === 1 ? "" : "s"}. Rotating creates a new token and lets the
          old one keep working for 24 hours so you can update your identity provider without a gap.
        </p>
      </CardContent>
      <CreateTokenModal
        open={creating}
        organizationId={organizationId}
        onClose={() => setCreating(false)}
        onCreated={(created) => {
          setCreating(false);
          setSecret(created);
          refresh();
        }}
      />
      <TokenSecretModal created={secret} onClose={() => setSecret(null)} />
      <ConfirmDialog
        open={revokeTarget !== null}
        title="Revoke this token?"
        description={`"${revokeTarget?.name}" stops working immediately. If your identity provider still uses it, provisioning will fail until you give it a new token.`}
        confirmLabel="Revoke token"
        destructive
        isLoading={revoke.isPending}
        onConfirm={() => revokeTarget && revoke.mutate(revokeTarget.id)}
        onCancel={() => setRevokeTarget(null)}
      />
      <ConfirmDialog
        open={rotateTarget !== null}
        title="Rotate this token?"
        description={`A new token is created and shown once. "${rotateTarget?.name}" keeps working for 24 hours, then expires.`}
        confirmLabel="Rotate token"
        isLoading={rotate.isPending}
        onConfirm={() => rotateTarget && rotate.mutate(rotateTarget.id)}
        onCancel={() => setRotateTarget(null)}
      />
    </Card>
  );
}

function PendingDeactivationsCard({ organizationId }: { organizationId: string }) {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [decision, setDecision] = useState<{ action: ScimPendingAction; kind: "approve" | "reject" } | null>(null);
  const query = useQuery({ queryKey: ["scim", "pending", organizationId], queryFn: () => listScimPending(organizationId) });
  const mutation = useMutation({
    mutationFn: ({ id, kind }: { id: string; kind: "approve" | "reject" }) => decideScimPending(organizationId, id, kind),
    onSuccess: (_result, { kind }) => {
      toast({ variant: "success", title: kind === "approve" ? "Deactivation approved" : "Deactivation rejected" });
      setDecision(null);
      void queryClient.invalidateQueries({ queryKey: ["scim"] });
      void queryClient.invalidateQueries({ queryKey: ["users"] });
    },
    onError: (error) => {
      toast({ variant: "error", title: "Couldn't record your decision", description: scimErrorMessage(error) });
      setDecision(null);
    },
  });
  const rows = query.data ?? [];
  if (query.isSuccess && rows.length === 0) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Deactivations awaiting approval</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p className="text-xs text-ink-muted">
          Your identity provider asked to deactivate more people in a short time than the safety limit allows, so these were
          held. Nothing has changed for them yet.
        </p>
        {query.isLoading && <LoadingState label="Loading…" />}
        {query.isError && <ErrorState title="Could not load held deactivations" onRetry={() => void query.refetch()} />}
        <ul className="divide-y divide-white/10" data-testid="scim-pending">
          {rows.map((p) => (
            <li key={p.id} className="flex flex-wrap items-center justify-between gap-3 py-2 text-sm">
              <div>
                <p className="font-medium text-ink">{p.userName}</p>
                <p className="text-xs text-ink-muted">
                  {p.reason} · {formatRelativeTime(p.createdAt)}
                </p>
              </div>
              <div className="flex gap-2">
                <Button size="sm" variant="danger" onClick={() => setDecision({ action: p, kind: "approve" })}>
                  Approve deactivation
                </Button>
                <Button size="sm" onClick={() => setDecision({ action: p, kind: "reject" })}>
                  Reject
                </Button>
              </div>
            </li>
          ))}
        </ul>
      </CardContent>
      <ConfirmDialog
        open={decision !== null}
        title={decision?.kind === "approve" ? "Approve this deactivation?" : "Reject this deactivation?"}
        description={
          decision?.kind === "approve"
            ? `${decision.action.userName} loses access to this organization immediately and is signed out.`
            : `${decision?.action.userName} keeps their access. Your identity provider may ask again on its next sync.`
        }
        confirmLabel={decision?.kind === "approve" ? "Approve deactivation" : "Reject"}
        destructive={decision?.kind === "approve"}
        isLoading={mutation.isPending}
        onConfirm={() => decision && mutation.mutate({ id: decision.action.id, kind: decision.kind })}
        onCancel={() => setDecision(null)}
      />
    </Card>
  );
}

function UnlinkedIdentitiesCard({ organizationId }: { organizationId: string }) {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [target, setTarget] = useState<ScimIdentity | null>(null);
  const [memberId, setMemberId] = useState("");
  const identities = useQuery({
    queryKey: ["scim", "identities", organizationId],
    queryFn: () => listScimIdentities(organizationId, "pending"),
  });
  const members = useQuery({ queryKey: ["users", organizationId], queryFn: () => listOrgUsers(organizationId), enabled: target !== null });
  const link = useMutation({
    mutationFn: () => linkScimIdentity(organizationId, target!.id, memberId),
    onSuccess: () => {
      toast({ variant: "success", title: "Identity linked" });
      setTarget(null);
      setMemberId("");
      void queryClient.invalidateQueries({ queryKey: ["scim"] });
      void queryClient.invalidateQueries({ queryKey: ["users"] });
    },
    onError: (error) => toast({ variant: "error", title: "Couldn't link the identity", description: scimErrorMessage(error) }),
  });
  const rows = identities.data?.items ?? [];
  if (identities.isSuccess && rows.length === 0) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Identities not linked to a member</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <Banner tone="info">
          Your identity provider has announced these people, but EKIP does not know which member each is. An email match is only a
          suggestion: nothing is linked, and nobody is deactivated, until you confirm it here (or the person signs in through SSO).
        </Banner>
        {identities.isLoading && <LoadingState label="Loading…" />}
        {identities.isError && <ErrorState title="Could not load identities" onRetry={() => void identities.refetch()} />}
        <ul className="divide-y divide-white/10" data-testid="scim-identities">
          {rows.map((i) => (
            <li key={i.id} className="flex flex-wrap items-center justify-between gap-3 py-2 text-sm">
              <div>
                <p className="font-medium text-ink">{i.userName}</p>
                <p className="text-xs text-ink-muted">
                  {i.active ? "Active in the identity provider" : "Inactive in the identity provider"}
                  {i.candidates.length > 0 && ` · possible match: ${i.candidates.map((c) => c.email).join(", ")}`}
                </p>
              </div>
              <Button size="sm" onClick={() => setTarget(i)}>
                Link to a member
              </Button>
            </li>
          ))}
        </ul>
      </CardContent>
      <Modal
        open={target !== null}
        onClose={() => setTarget(null)}
        title="Link to a member"
        description={target ? `Identity: ${target.userName}` : undefined}
      >
        <form
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (memberId) link.mutate();
          }}
        >
          <div>
            <label htmlFor="scim-link-member" className="mb-1.5 block text-xs font-medium text-ink-muted">
              Member
            </label>
            <Select id="scim-link-member" value={memberId} onChange={(e) => setMemberId(e.target.value)}>
              <option value="">Choose a member…</option>
              {(members.data ?? []).map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name} ({m.email})
                </option>
              ))}
            </Select>
          </div>
          <p className="text-xs text-ink-muted">
            From now on, deactivating or deleting this person in your identity provider deactivates this member&apos;s access to
            this organization.
            {target && !target.active && " The identity provider already reports them inactive, so they will be deactivated immediately."}
          </p>
          <div className="flex justify-end gap-2">
            <Button type="button" size="sm" onClick={() => setTarget(null)}>
              Cancel
            </Button>
            <Button type="submit" size="sm" variant="primary" isLoading={link.isPending} disabled={!memberId}>
              Link identity
            </Button>
          </div>
        </form>
      </Modal>
    </Card>
  );
}

function GroupsCard({ organizationId }: { organizationId: string }) {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [target, setTarget] = useState<ScimGroup | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const groups = useQuery({ queryKey: ["scim", "groups", organizationId], queryFn: () => listScimGroups(organizationId) });
  const roles = useQuery({ queryKey: ["roles", organizationId], queryFn: () => listRoles(organizationId), enabled: target !== null });
  const save = useMutation({
    mutationFn: () => setScimGroupRoles(organizationId, target!.id, selected),
    onSuccess: () => {
      toast({ variant: "success", title: "Group roles saved" });
      setTarget(null);
      void queryClient.invalidateQueries({ queryKey: ["scim", "groups"] });
      void queryClient.invalidateQueries({ queryKey: ["users"] });
    },
    onError: (error) => toast({ variant: "error", title: "Couldn't save the group's roles", description: scimErrorMessage(error) }),
  });
  const rows = groups.data ?? [];
  if (groups.isSuccess && rows.length === 0) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Groups and roles</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <Banner tone="info">
          A group from your identity provider grants nothing until you choose its roles here. Removing someone from a group withdraws
          only the roles that group granted. Roles you assigned by hand are never removed, and a member is deactivated only if they
          would be left with no role at all.
        </Banner>
        {groups.isLoading && <LoadingState label="Loading…" />}
        {groups.isError && <ErrorState title="Could not load groups" onRetry={() => void groups.refetch()} />}
        <ul className="divide-y divide-white/10" data-testid="scim-groups">
          {rows.map((g) => (
            <li key={g.id} className="flex flex-wrap items-center justify-between gap-3 py-2 text-sm">
              <div>
                <p className="font-medium text-ink">{g.displayName}</p>
                <p className="text-xs text-ink-muted">
                  {g.memberCount} {g.memberCount === 1 ? "member" : "members"} ·{" "}
                  {g.roles.length > 0 ? `grants ${g.roles.map((r) => r.name).join(", ")}` : "grants no roles yet"}
                </p>
              </div>
              <Button
                size="sm"
                onClick={() => {
                  setTarget(g);
                  setSelected(g.roles.map((r) => r.id));
                }}
              >
                Choose roles
              </Button>
            </li>
          ))}
        </ul>
      </CardContent>
      <Modal
        open={target !== null}
        onClose={() => setTarget(null)}
        title="Roles granted by this group"
        description={target ? `Group: ${target.displayName}` : undefined}
      >
        <form
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate();
          }}
        >
          {roles.isLoading && <LoadingState label="Loading roles…" />}
          {roles.isError && <ErrorState title="Could not load roles" onRetry={() => void roles.refetch()} />}
          <ul className="flex flex-col gap-2">
            {(roles.data ?? []).map((r) => (
              <li key={r.id}>
                <label className="flex items-center gap-2 text-sm text-ink">
                  <input
                    type="checkbox"
                    checked={selected.includes(r.id)}
                    onChange={() => setSelected((cur) => (cur.includes(r.id) ? cur.filter((x) => x !== r.id) : [...cur, r.id]))}
                  />
                  {r.name}
                </label>
              </li>
            ))}
          </ul>
          <p className="text-xs text-ink-muted">
            Saving applies immediately: members of the group gain these roles, and roles you remove here are withdrawn from members who
            held them only through this group. You can only choose roles whose permissions you hold yourself.
          </p>
          <div className="flex justify-end gap-2">
            <Button type="button" size="sm" onClick={() => setTarget(null)}>
              Cancel
            </Button>
            <Button type="submit" size="sm" variant="primary" isLoading={save.isPending}>
              Save roles
            </Button>
          </div>
        </form>
      </Modal>
    </Card>
  );
}

function SafetySettingsCard({ organizationId, overview }: { organizationId: string; overview: ScimOverview }) {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [min, setMin] = useState(String(overview.guardMinAbsolute));
  const [percent, setPercent] = useState(String(overview.guardPercent));
  const [windowMinutes, setWindowMinutes] = useState(String(overview.guardWindowMinutes));
  const [settle, setSettle] = useState(String(overview.reactivationSettleSeconds));
  const mutation = useMutation({
    mutationFn: () =>
      updateScimConfig(organizationId, {
        guardMinAbsolute: Number(min),
        guardPercent: Number(percent),
        guardWindowMinutes: Number(windowMinutes),
        reactivationSettleSeconds: Number(settle),
      }),
    onSuccess: () => {
      toast({ variant: "success", title: "Safety settings saved" });
      void queryClient.invalidateQueries({ queryKey: ["scim"] });
    },
    onError: (error) => toast({ variant: "error", title: "Couldn't save the settings", description: scimErrorMessage(error) }),
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Safety limits</CardTitle>
      </CardHeader>
      <CardContent>
        <form
          className="grid max-w-xl grid-cols-1 gap-4 sm:grid-cols-2"
          onSubmit={(event) => {
            event.preventDefault();
            mutation.mutate();
          }}
        >
          <div className="sm:col-span-2 text-xs text-ink-muted">
            Deactivations your identity provider requests beyond these limits are held for your approval instead of being applied.
            The organization&apos;s last administrator can never be deactivated through SCIM.
          </div>
          <div>
            <label htmlFor="guard-min" className="mb-1.5 block text-xs font-medium text-ink-muted">
              At least this many per window
            </label>
            <Input id="guard-min" type="number" min={1} value={min} onChange={(e) => setMin(e.target.value)} />
          </div>
          <div>
            <label htmlFor="guard-percent" className="mb-1.5 block text-xs font-medium text-ink-muted">
              Or this percent of active members
            </label>
            <Input id="guard-percent" type="number" min={1} max={100} value={percent} onChange={(e) => setPercent(e.target.value)} />
          </div>
          <div>
            <label htmlFor="guard-window" className="mb-1.5 block text-xs font-medium text-ink-muted">
              Window (minutes)
            </label>
            <Input id="guard-window" type="number" min={1} max={1440} value={windowMinutes} onChange={(e) => setWindowMinutes(e.target.value)} />
          </div>
          <div>
            <label htmlFor="guard-settle" className="mb-1.5 block text-xs font-medium text-ink-muted">
              Re-enable settle time (seconds)
            </label>
            <Input id="guard-settle" type="number" min={0} value={settle} onChange={(e) => setSettle(e.target.value)} />
          </div>
          <div className="sm:col-span-2">
            <Button type="submit" size="sm" variant="primary" isLoading={mutation.isPending}>
              Save safety limits
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------------------------- page

export function ProvisioningSettingsPage() {
  const { organization, isLoading: isTenantLoading } = useTenant();
  const { user } = useAuth();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const canManage = Boolean(user?.permissions.includes("tenancy:manage"));
  const organizationId = organization?.id ?? "";

  const overviewQuery = useQuery({
    queryKey: ["scim", "overview", organizationId],
    queryFn: () => getScimOverview(organizationId),
    enabled: Boolean(organization) && canManage,
    retry: false,
  });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => updateScimConfig(organizationId, { enabled }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["scim"] }),
    onError: (error) => toast({ variant: "error", title: "Couldn't change the setting", description: scimErrorMessage(error) }),
  });

  if (isTenantLoading || (canManage && overviewQuery.isLoading)) return <LoadingState label="Loading provisioning settings…" />;
  if (!canManage || (overviewQuery.error as ApiError | null)?.status === 403) {
    return (
      <ErrorState
        title="You don't have access to provisioning settings"
        description="Managing SCIM provisioning requires the tenancy:manage permission."
      />
    );
  }
  if (overviewQuery.isError || !overviewQuery.data) {
    return <ErrorState title="Could not load provisioning settings" onRetry={() => void overviewQuery.refetch()} />;
  }

  const overview = overviewQuery.data;
  const health = syncHealth(overview);

  return (
    <div className="flex flex-col gap-4" data-testid="provisioning-page">
      {!overview.platformEnabled && (
        <Banner tone="warning" testId="scim-platform-off">
          SCIM provisioning is switched off for this deployment, so nothing below can take effect. Ask whoever runs EKIP to enable it.
        </Banner>
      )}

      <Card>
        <CardHeader>
          <CardTitle>SCIM provisioning</CardTitle>
          <label className="flex items-center gap-2 text-sm text-ink">
            <input
              type="checkbox"
              checked={overview.enabled}
              disabled={toggle.isPending}
              onChange={(e) => toggle.mutate(e.target.checked)}
              aria-label="Enable SCIM provisioning"
            />
            {overview.enabled ? "Enabled" : "Disabled"}
          </label>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <p className="text-xs text-ink-muted">
            Let Microsoft Entra ID or Okta tell EKIP when someone leaves. Deactivating or deleting them in your identity provider
            deactivates their access to <strong>this organization only</strong>. Nothing is deleted, and their account elsewhere is
            untouched.
          </p>
          <CopyField label="SCIM base URL" value={overview.endpointUrl} id="scim-endpoint" />
          <p className="text-xs text-ink-subtle">
            Use this as the tenant / base URL, with a token below as the secret token, in your identity provider&apos;s SCIM
            connection. Only deprovisioning is supported: EKIP does not create accounts from SCIM.
          </p>
          <div className="flex flex-wrap items-center gap-3 text-xs" data-testid="scim-health">
            <Badge tone={HEALTH_TONE[health.level]} data-testid="scim-health-level" data-level={health.level}>
              {health.level === "ok" ? "Healthy" : health.level === "off" ? "Off" : health.level === "stale" ? "Stale" : health.level === "error" ? "Error" : health.level === "no_token" ? "No token" : "No requests yet"}
            </Badge>
            <span className="text-ink-muted">{health.message}</span>
          </div>
          <dl className="grid grid-cols-1 gap-3 text-xs sm:grid-cols-3">
            <div>
              <dt className="text-ink-subtle">Last request</dt>
              <dd className="text-ink" data-testid="scim-last-request">{overview.lastRequestAt ? formatRelativeTime(overview.lastRequestAt) : "never"}</dd>
            </div>
            <div>
              <dt className="text-ink-subtle">Last successful request</dt>
              <dd className="text-ink" data-testid="scim-last-success">{overview.lastSuccessAt ? formatRelativeTime(overview.lastSuccessAt) : "never"}</dd>
            </div>
            <div>
              <dt className="text-ink-subtle">Last error</dt>
              <dd className="text-ink" data-testid="scim-last-error">
                {overview.lastErrorCode ? `${overview.lastErrorCode}${overview.lastErrorAt ? ` · ${formatRelativeTime(overview.lastErrorAt)}` : ""}` : "none"}
              </dd>
            </div>
          </dl>
          {(health.level === "stale" || health.level === "error" || health.level === "no_token") && (
            <Banner tone={health.level === "error" ? "critical" : "warning"} testId="scim-health-warning">
              {health.message} People removed in your identity provider keep their EKIP access until synchronization works again.
              You can deactivate anyone by hand under Settings → Users.
            </Banner>
          )}
        </CardContent>
      </Card>

      <PendingDeactivationsCard organizationId={organizationId} />
      <TokensCard organizationId={organizationId} overview={overview} />
      <UnlinkedIdentitiesCard organizationId={organizationId} />
      <GroupsCard organizationId={organizationId} />
      <SafetySettingsCard organizationId={organizationId} overview={overview} key={`${overview.guardMinAbsolute}-${overview.guardPercent}-${overview.guardWindowMinutes}-${overview.reactivationSettleSeconds}`} />
    </div>
  );
}
