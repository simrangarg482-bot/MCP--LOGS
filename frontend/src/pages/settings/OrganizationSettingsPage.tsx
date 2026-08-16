import type { ReactNode } from "react";
import { useTenant } from "@/context/TenantContext";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";

// There is no `update_organization` backend capability -- `core.tenancy.
// service`/`app.api.routers.tenancy` only support creating and reading an
// organization, never editing one after creation. This view is read-only
// for all fields; do not add a "Save changes" action here without a real
// backend endpoint to back it.
export function OrganizationSettingsPage() {
  const { organization } = useTenant();

  return (
    <Card>
      <CardHeader>
        <CardTitle>Organization</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <Field label="Organization name" id="org-name">
          <Input id="org-name" defaultValue={organization?.name} disabled />
        </Field>
        <Field label="Slug" id="org-slug">
          <Input id="org-slug" defaultValue={organization?.slug} disabled />
        </Field>
        <Field label="Organization ID" id="org-id">
          <Input id="org-id" defaultValue={organization?.id} disabled className="font-mono text-xs" />
        </Field>
        <p className="text-xs text-ink-subtle">
          Organization details cannot be edited after creation.
        </p>
      </CardContent>
    </Card>
  );
}

function Field({ label, id, children }: { label: string; id: string; children: ReactNode }) {
  return (
    <div className="max-w-md">
      <label htmlFor={id} className="mb-1.5 block text-xs font-medium text-ink-muted">{label}</label>
      {children}
    </div>
  );
}
