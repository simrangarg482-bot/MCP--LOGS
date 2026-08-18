import { useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Select } from "@/components/ui/Select";
import { Input } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { LoadingState } from "@/components/ui/LoadingState";
import { ErrorState } from "@/components/ui/ErrorState";
import { getSsoConfig, updateSsoConfig } from "@/api/tenancy";
import type { SsoConfig, SsoProviderKind, SsoProtocol } from "@/types/tenancy";
import type { ApiError } from "@/types/common";
import { useToast } from "@/context/ToastContext";
import { useTenant } from "@/context/TenantContext";
import { useAuth } from "@/context/AuthContext";

const BLANK_SSO_CONFIG: SsoConfig = {
  provider: "entra_id",
  protocol: "oidc",
  issuerUrl: "",
  clientId: "",
  clientSecretReference: "",
  enabled: false,
};

// Matches app/core/tenancy/schemas.py's SSOProvider literal exactly -- the
// real backend rejects anything else with a 422.
const PROVIDERS: { value: SsoProviderKind; label: string }[] = [
  { value: "entra_id", label: "Microsoft Entra ID" },
  { value: "okta", label: "Okta" },
  { value: "auth0", label: "Auth0" },
  { value: "google_workspace", label: "Google Workspace" },
];

const PROTOCOLS: SsoProtocol[] = ["oidc", "saml"];

export function SsoSettingsPage() {
  const { toast } = useToast();
  const { organization, isLoading: isTenantLoading } = useTenant();
  const { user } = useAuth();
  // Mirrors `core.tenancy.service.configure_sso`'s real `tenancy:manage`
  // gate -- UX only, the backend re-checks regardless.
  const canManage = Boolean(user?.permissions.includes("tenancy:manage"));
  const organizationId = organization?.id ?? "";
  const ssoQuery = useQuery({
    queryKey: ["sso", organizationId],
    queryFn: () => getSsoConfig(organizationId),
    enabled: Boolean(organizationId),
  });
  const [draft, setDraft] = useState<SsoConfig | null>(null);

  const queryError = ssoQuery.error as ApiError | null;
  // "Not configured yet" isn't a real failure -- it's the normal state for
  // an organization that hasn't set up SSO, and (for a manager) the entry
  // point into configuring it for the first time via the same form below.
  const notConfiguredYet = queryError?.errorCode === "sso_configuration.not_found";
  // `core.tenancy.service.configure_sso` is create-only -- it raises
  // ConflictError if a configuration already exists (see its own
  // docstring: "replacing an existing configuration is a distinct,
  // not-yet-built operation"), and there is no PUT/PATCH endpoint to
  // invent one for. An already-configured org's fields are therefore
  // rendered read-only below, never fed into an editable `draft` that
  // could be resubmitted to the create-only endpoint.
  const isAlreadyConfigured = Boolean(ssoQuery.data);
  const config = isAlreadyConfigured ? ssoQuery.data! : draft ?? (notConfiguredYet ? BLANK_SSO_CONFIG : null);

  const saveMutation = useMutation({
    mutationFn: (next: SsoConfig) => updateSsoConfig(organizationId, next),
    onSuccess: () => toast({ variant: "success", title: "SSO configuration saved" }),
    onError: () => toast({ variant: "error", title: "Failed to save SSO configuration" }),
  });

  // `organizationId` is empty (and `ssoQuery` correctly disabled) for a
  // real, if usually brief, window while TenantContext's own `listOrganizations`
  // call is in flight -- a bare `ssoQuery.isLoading` check misses that window
  // entirely (a disabled query is never "loading"), so visiting this page
  // directly (not via an in-app nav click that's already past it) previously
  // rendered a silent blank page instead of a loading state.
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
  if (!config) return null;

  function update<K extends keyof SsoConfig>(key: K, value: SsoConfig[K]) {
    setDraft({ ...config, [key]: value } as SsoConfig);
  }

  // Fields are only ever editable while creating a brand-new configuration
  // (`notConfiguredYet`); an already-configured org's fields are read-only
  // (see `isAlreadyConfigured`'s docstring above) -- there is no backend
  // capability to change them once set.
  const fieldsDisabled = !canManage || isAlreadyConfigured;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Single sign-on</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {!canManage && (
          <p className="rounded-md border border-border bg-slate-50 px-3 py-2 text-xs text-ink-muted">
            You need the <span className="font-medium text-ink">tenancy:manage</span> permission to change SSO
            configuration. You can still view the current settings below.
          </p>
        )}
        {canManage && isAlreadyConfigured && (
          <p className="rounded-md border border-border bg-slate-50 px-3 py-2 text-xs text-ink-muted">
            SSO is already configured for this organization. Changing an existing configuration isn't supported yet
            -- these fields are shown read-only.
          </p>
        )}

        <div className="max-w-md">
          <label htmlFor="sso-provider" className="mb-1.5 block text-xs font-medium text-ink-muted">Provider</label>
          <Select
            id="sso-provider"
            disabled={fieldsDisabled}
            value={config.provider}
            onChange={(e) => update("provider", e.target.value as SsoProviderKind)}
          >
            {PROVIDERS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </Select>
        </div>

        <div className="max-w-md">
          <label htmlFor="sso-protocol" className="mb-1.5 block text-xs font-medium text-ink-muted">Protocol</label>
          <Select
            id="sso-protocol"
            disabled={fieldsDisabled}
            value={config.protocol}
            onChange={(e) => update("protocol", e.target.value as SsoProtocol)}
          >
            {PROTOCOLS.map((p) => (
              <option key={p} value={p}>
                {p.toUpperCase()}
              </option>
            ))}
          </Select>
        </div>

        <div className="max-w-md">
          <label htmlFor="sso-issuer-url" className="mb-1.5 block text-xs font-medium text-ink-muted">Issuer URL</label>
          <Input id="sso-issuer-url" disabled={fieldsDisabled} value={config.issuerUrl} onChange={(e) => update("issuerUrl", e.target.value)} />
        </div>

        <div className="max-w-md">
          <label htmlFor="sso-client-id" className="mb-1.5 block text-xs font-medium text-ink-muted">Client ID</label>
          <Input id="sso-client-id" disabled={fieldsDisabled} value={config.clientId} onChange={(e) => update("clientId", e.target.value)} />
        </div>

        <div className="max-w-md">
          <label htmlFor="sso-client-secret" className="mb-1.5 block text-xs font-medium text-ink-muted">Client secret</label>
          {isAlreadyConfigured ? (
            <Input id="sso-client-secret" disabled value={config.clientSecretReference} className="font-mono text-xs" />
          ) : (
            <Input
              id="sso-client-secret"
              type="password"
              disabled={fieldsDisabled}
              value={config.clientSecretReference}
              onChange={(e) => update("clientSecretReference", e.target.value)}
              autoComplete="new-password"
              className="font-mono text-xs"
            />
          )}
          <p className="mt-1 text-xs text-ink-subtle">
            {isAlreadyConfigured
              ? "Encrypted at rest. The real value is never sent back to the browser after saving."
              : "Entered once and encrypted at rest server-side -- it won't be shown or editable again after saving."}
          </p>
        </div>

        {!isAlreadyConfigured && (
          <div>
            <Button
              variant="primary"
              size="sm"
              isLoading={saveMutation.isPending}
              disabled={!canManage}
              onClick={() => saveMutation.mutate(config)}
            >
              Save configuration
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
