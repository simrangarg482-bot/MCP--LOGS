import { useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Select } from "@/components/ui/Select";
import { Input } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { LoadingState } from "@/components/ui/LoadingState";
import { getSsoConfig, updateSsoConfig } from "@/api/tenancy";
import type { SsoConfig, SsoProviderKind, SsoProtocol } from "@/types/tenancy";
import { useToast } from "@/context/ToastContext";
import { useTenant } from "@/context/TenantContext";
import { useAuth } from "@/context/AuthContext";

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
  const { organization } = useTenant();
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

  const config = draft ?? ssoQuery.data ?? null;

  const saveMutation = useMutation({
    mutationFn: (next: SsoConfig) => updateSsoConfig(organizationId, next),
    onSuccess: () => toast({ variant: "success", title: "SSO configuration saved" }),
    onError: () => toast({ variant: "error", title: "Failed to save SSO configuration" }),
  });

  if (ssoQuery.isLoading) return <LoadingState label="Loading SSO configuration…" />;
  if (!config) return null;

  function update<K extends keyof SsoConfig>(key: K, value: SsoConfig[K]) {
    setDraft({ ...config, [key]: value } as SsoConfig);
  }

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

        <div className="max-w-md">
          <label htmlFor="sso-provider" className="mb-1.5 block text-xs font-medium text-ink-muted">Provider</label>
          <Select
            id="sso-provider"
            disabled={!canManage}
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
            disabled={!canManage}
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
          <Input id="sso-issuer-url" disabled={!canManage} value={config.issuerUrl} onChange={(e) => update("issuerUrl", e.target.value)} />
        </div>

        <div className="max-w-md">
          <label htmlFor="sso-client-id" className="mb-1.5 block text-xs font-medium text-ink-muted">Client ID</label>
          <Input id="sso-client-id" disabled={!canManage} value={config.clientId} onChange={(e) => update("clientId", e.target.value)} />
        </div>

        <div className="max-w-md">
          <label htmlFor="sso-client-secret" className="mb-1.5 block text-xs font-medium text-ink-muted">Client secret reference</label>
          <Input
            id="sso-client-secret"
            disabled={!canManage}
            value={config.clientSecretReference}
            onChange={(e) => update("clientSecretReference", e.target.value)}
            className="font-mono text-xs"
          />
          <p className="mt-1 text-xs text-ink-subtle">
            Reference to a secret manager entry. Actual secret values are never displayed or stored client-side.
          </p>
        </div>

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
      </CardContent>
    </Card>
  );
}
