import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Copy } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Select } from "@/components/ui/Select";
import { Input } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { LoadingState } from "@/components/ui/LoadingState";
import { ErrorState } from "@/components/ui/ErrorState";
import { createSsoConfig, getSsoConfig, patchSsoConfig, type SsoConfigChanges } from "@/api/tenancy";
import type { SsoConfig, SsoProviderKind } from "@/types/tenancy";
import type { ApiError } from "@/types/common";
import { useToast } from "@/context/ToastContext";
import { useTenant } from "@/context/TenantContext";
import { useAuth } from "@/context/AuthContext";
import { providerBrand, ssoCallbackUrl, ssoLoginUrl } from "@/utils/sso";

// Matches app/core/tenancy/schemas.py's SSOProvider literal exactly -- the real
// backend rejects anything else with a 422. SAML is not implemented, so it is not offered.
const PROVIDERS: { value: SsoProviderKind; label: string }[] = [
  { value: "entra_id", label: "Microsoft Entra ID" },
  { value: "okta", label: "Okta" },
  { value: "auth0", label: "Auth0" },
  { value: "google_workspace", label: "Google Workspace" },
];

interface Draft {
  provider: SsoProviderKind;
  issuerUrl: string;
  clientId: string;
  clientSecret: string;
}

const BLANK_DRAFT: Draft = { provider: "entra_id", issuerUrl: "", clientId: "", clientSecret: "" };

/** The server's sentence for a failed save, falling back to a generic one. */
function saveErrorMessage(error: unknown): string {
  const apiError = error as Partial<ApiError> | undefined;
  switch (apiError?.errorCode) {
    case "sso.issuer_invalid":
      return "The issuer URL isn't acceptable. It must be an https:// address with no query string or credentials.";
    case "sso.protocol_unsupported":
      return "Only OpenID Connect (OIDC) is supported.";
    case "sso.client_secret_required":
      return "Enter the client secret.";
    case "sso.field_required":
      return "Fill in every field.";
    case "sso.issuer_invalid_host":
      return "The issuer URL isn't reachable.";
    case "permission_denied":
      return "You need the tenancy:manage permission to change SSO.";
    case "sso_configuration.kms_unavailable":
      return "The secret store is unavailable right now. Try again shortly.";
    default:
      return apiError?.message || "Failed to save the SSO configuration.";
  }
}

function CopyField({ label, value, id }: { label: string; value: string; id: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="max-w-xl">
      <label htmlFor={id} className="mb-1.5 block text-xs font-medium text-ink-muted">
        {label}
      </label>
      <div className="flex gap-2">
        <Input id={id} readOnly value={value} className="font-mono text-xs" onFocus={(e) => e.currentTarget.select()} />
        <Button
          type="button"
          size="sm"
          variant="secondary"
          aria-label={`Copy ${label}`}
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(value);
              setCopied(true);
              window.setTimeout(() => setCopied(false), 1500);
            } catch {
              /* clipboard unavailable: the field is selectable */
            }
          }}
        >
          {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
    </div>
  );
}

export function SsoSettingsPage() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const { organization, isLoading: isTenantLoading } = useTenant();
  const { user } = useAuth();
  // Mirrors the real `tenancy:manage` gate -- UX only, the backend re-checks regardless.
  const canManage = Boolean(user?.permissions.includes("tenancy:manage"));
  const organizationId = organization?.id ?? "";
  const ssoQuery = useQuery({
    queryKey: ["sso", organizationId],
    queryFn: () => getSsoConfig(organizationId),
    enabled: Boolean(organizationId),
    retry: false,
  });
  const config = ssoQuery.data ?? null;

  const [draft, setDraft] = useState<Draft>(BLANK_DRAFT);
  const [editing, setEditing] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [confirmDisable, setConfirmDisable] = useState(false);
  // Set when the server says the change would unlink people and wants an explicit yes.
  const [resetPrompt, setResetPrompt] = useState<{ count: number; changes: SsoConfigChanges } | null>(null);

  const queryError = ssoQuery.error as ApiError | null;
  // "Not configured yet" isn't a failure: it's the normal first-run state.
  const notConfiguredYet = queryError?.errorCode === "sso_configuration.not_found";

  const afterSave = (saved: SsoConfig, message: string) => {
    queryClient.setQueryData(["sso", organizationId], saved);
    setEditing(false);
    setFormError(null);
    setResetPrompt(null);
    setConfirmDisable(false);
    toast({ variant: "success", title: message });
  };

  const createMutation = useMutation({
    mutationFn: () =>
      createSsoConfig(organizationId, {
        provider: draft.provider,
        protocol: "oidc",
        issuerUrl: draft.issuerUrl.trim(),
        clientId: draft.clientId.trim(),
        clientSecretReference: draft.clientSecret,
        enabled: true,
      }),
    onSuccess: (saved) => {
      setDraft(BLANK_DRAFT);
      afterSave(saved, "SSO configured");
    },
    onError: (error) => setFormError(saveErrorMessage(error)),
  });

  const patchMutation = useMutation({
    mutationFn: (changes: SsoConfigChanges) => patchSsoConfig(organizationId, changes),
    onSuccess: (saved, changes) =>
      afterSave(
        saved,
        changes.enabled === false ? "SSO disabled" : changes.enabled === true && Object.keys(changes).length === 1 ? "SSO enabled" : "SSO configuration saved",
      ),
    onError: (error, changes) => {
      const apiError = error as unknown as ApiError;
      setConfirmDisable(false);
      if (apiError.errorCode === "sso.identity_reset_required") {
        const detail = (apiError.detail ?? {}) as { identity_link_count?: number; identityLinkCount?: number };
        setResetPrompt({ count: Number(detail.identity_link_count ?? detail.identityLinkCount ?? 0), changes });
        return;
      }
      setFormError(saveErrorMessage(error));
      toast({ variant: "error", title: "Couldn't save the SSO configuration" });
    },
  });

  if (isTenantLoading || ssoQuery.isLoading) return <LoadingState label="Loading SSO configuration…" />;
  if (queryError?.status === 403) {
    return (
      <ErrorState
        title="You don't have access to SSO configuration"
        description="Viewing single sign-on settings requires the tenancy:manage permission."
      />
    );
  }
  if (ssoQuery.isError && !notConfiguredYet) {
    return <ErrorState onRetry={() => ssoQuery.refetch()} />;
  }

  const slug = organization?.slug ?? "";
  const field = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((d) => ({ ...d, [key]: value }));

  function startEditing() {
    if (!config) return;
    setDraft({ provider: config.provider, issuerUrl: config.issuerUrl, clientId: config.clientId, clientSecret: "" });
    setFormError(null);
    setEditing(true);
  }

  function saveEdits() {
    if (!config) return;
    const changes: SsoConfigChanges = {};
    if (draft.provider !== config.provider) changes.provider = draft.provider;
    if (draft.issuerUrl.trim() !== config.issuerUrl) changes.issuerUrl = draft.issuerUrl.trim();
    if (draft.clientId.trim() !== config.clientId) changes.clientId = draft.clientId.trim();
    if (draft.clientSecret) changes.clientSecret = draft.clientSecret;
    if (Object.keys(changes).length === 0) {
      setEditing(false);
      return;
    }
    setFormError(null);
    patchMutation.mutate(changes);
  }

  const providerLabel = config ? PROVIDERS.find((p) => p.value === config.provider)?.label ?? config.provider : "";

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            Single sign-on
            {config && (
              <Badge tone={config.enabled ? "success" : "neutral"} data-testid="sso-status">
                {config.enabled ? "Enabled" : "Disabled"}
              </Badge>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {!canManage && (
            <p className="rounded-md border border-border bg-white/[0.03] px-3 py-2 text-xs text-ink-muted">
              You need the <span className="font-medium text-ink">tenancy:manage</span> permission to change SSO
              configuration. You can still view the current settings below.
            </p>
          )}

          {config && !editing && (
            <>
              <dl className="grid max-w-xl grid-cols-[8rem_1fr] gap-x-4 gap-y-2 text-sm">
                <dt className="text-ink-muted">Provider</dt>
                <dd className="text-ink" data-testid="sso-provider">{providerLabel}</dd>
                <dt className="text-ink-muted">Protocol</dt>
                <dd className="text-ink">OpenID Connect (OIDC)</dd>
                <dt className="text-ink-muted">Issuer URL</dt>
                <dd className="break-all font-mono text-xs text-ink">{config.issuerUrl}</dd>
                <dt className="text-ink-muted">Client ID</dt>
                <dd className="break-all font-mono text-xs text-ink">{config.clientId}</dd>
                <dt className="text-ink-muted">Client secret</dt>
                <dd className="text-xs text-ink-subtle">Encrypted at rest. Never sent back to the browser.</dd>
              </dl>
              {canManage && (
                <div className="flex gap-2">
                  <Button size="sm" variant="secondary" onClick={startEditing}>
                    Edit configuration
                  </Button>
                  {config.enabled ? (
                    <Button size="sm" variant="danger" onClick={() => setConfirmDisable(true)}>
                      Disable SSO
                    </Button>
                  ) : (
                    <Button size="sm" variant="primary" isLoading={patchMutation.isPending} onClick={() => patchMutation.mutate({ enabled: true })}>
                      Enable SSO
                    </Button>
                  )}
                </div>
              )}
            </>
          )}

          {((!config && notConfiguredYet) || editing) && (
            <form
              className="flex flex-col gap-4"
              onSubmit={(event) => {
                event.preventDefault();
                if (editing) saveEdits();
                else createMutation.mutate();
              }}
            >
              <div className="max-w-md">
                <label htmlFor="sso-provider" className="mb-1.5 block text-xs font-medium text-ink-muted">Provider</label>
                <Select
                  id="sso-provider"
                  disabled={!canManage}
                  value={draft.provider}
                  onChange={(e) => field("provider", e.target.value as SsoProviderKind)}
                >
                  {PROVIDERS.map((p) => (
                    <option key={p.value} value={p.value}>
                      {p.label}
                    </option>
                  ))}
                </Select>
              </div>
              <div className="max-w-md">
                <label htmlFor="sso-issuer-url" className="mb-1.5 block text-xs font-medium text-ink-muted">Issuer URL</label>
                <Input id="sso-issuer-url" required disabled={!canManage} placeholder="https://your-tenant.okta.com" value={draft.issuerUrl} onChange={(e) => field("issuerUrl", e.target.value)} />
              </div>
              <div className="max-w-md">
                <label htmlFor="sso-client-id" className="mb-1.5 block text-xs font-medium text-ink-muted">Client ID</label>
                <Input id="sso-client-id" required disabled={!canManage} value={draft.clientId} onChange={(e) => field("clientId", e.target.value)} />
              </div>
              <div className="max-w-md">
                <label htmlFor="sso-client-secret" className="mb-1.5 block text-xs font-medium text-ink-muted">Client secret</label>
                <Input
                  id="sso-client-secret"
                  type="password"
                  required={!editing}
                  disabled={!canManage}
                  value={draft.clientSecret}
                  onChange={(e) => field("clientSecret", e.target.value)}
                  autoComplete="new-password"
                  placeholder={editing ? "Leave blank to keep the current secret" : ""}
                  className="font-mono text-xs"
                />
                <p className="mt-1 text-xs text-ink-subtle">
                  Entered once and encrypted at rest server-side -- it won&apos;t be shown again after saving.
                </p>
              </div>
              {formError && (
                <p role="alert" className="max-w-md rounded-md border border-critical-border bg-critical-subtle px-3 py-2 text-xs text-critical">
                  {formError}
                </p>
              )}
              <div className="flex gap-2">
                <Button type="submit" variant="primary" size="sm" isLoading={createMutation.isPending || patchMutation.isPending} disabled={!canManage}>
                  {editing ? "Save changes" : "Save configuration"}
                </Button>
                {editing && (
                  <Button type="button" size="sm" variant="secondary" onClick={() => setEditing(false)}>
                    Cancel
                  </Button>
                )}
              </div>
            </form>
          )}
        </CardContent>
      </Card>

      {slug && (
        <Card>
          <CardHeader>
            <CardTitle>Sign-in links</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <CopyField id="sso-login-url" label="Organization SSO login URL" value={ssoLoginUrl(slug)} />
            <p className="max-w-xl text-xs text-ink-subtle">
              Share this link with your team. Anyone can also choose <span className="text-ink-muted">Sign in with your organization</span> on
              the sign-in page and enter <span className="font-mono text-ink-muted">{slug}</span>.
              {config && !config.enabled && " SSO is currently disabled, so the link will say it isn't available."}
            </p>
            <CopyField id="sso-callback-url" label="Redirect URI to register at your identity provider" value={ssoCallbackUrl(slug)} />
            <p className="max-w-xl text-xs text-ink-subtle">
              In {config ? providerBrand(config.provider).name : "your identity provider"}, add this exact address as an allowed redirect
              (callback) URI for the client above. Use the HTTPS address of your deployed app in production.
            </p>
          </CardContent>
        </Card>
      )}

      <ConfirmDialog
        open={confirmDisable}
        destructive
        title="Disable single sign-on?"
        description="People will no longer be able to sign in with SSO, and logins already in progress will fail. Their accounts and identity links are kept, and you can enable SSO again at any time. Password sign-in is not affected."
        confirmLabel="Disable SSO"
        isLoading={patchMutation.isPending}
        onCancel={() => setConfirmDisable(false)}
        onConfirm={() => patchMutation.mutate({ enabled: false })}
      />
      <ConfirmDialog
        open={resetPrompt !== null}
        destructive
        title="Switch identity provider?"
        description={`Changing the provider or issuer unlinks ${resetPrompt?.count ?? 0} ${resetPrompt?.count === 1 ? "person" : "people"} who signed in with SSO. They are matched again on their next sign-in (by invitation, domain or group rule). Continue?`}
        confirmLabel="Unlink and save"
        isLoading={patchMutation.isPending}
        onCancel={() => setResetPrompt(null)}
        onConfirm={() => resetPrompt && patchMutation.mutate({ ...resetPrompt.changes, confirmIdentityReset: true })}
      />
    </div>
  );
}
