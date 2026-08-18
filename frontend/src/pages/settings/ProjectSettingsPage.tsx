import { useTenant } from "@/context/TenantContext";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Badge } from "@/components/ui/Badge";
import { formatDate } from "@/utils/date";

// There is no `update_project` backend capability -- `core.tenancy.service`
// only supports creating and listing projects, never editing one after
// creation. This view is read-only; do not add a "Save changes" action here
// without a real backend endpoint to back it.
export function ProjectSettingsPage() {
  const { project, projects, organization } = useTenant();

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Active project</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="max-w-md">
            <label htmlFor="project-name" className="mb-1.5 block text-xs font-medium text-ink-muted">Project name</label>
            <Input id="project-name" defaultValue={project?.name} disabled />
          </div>
          <div className="flex items-center gap-2 text-xs text-ink-muted">
            {project?.isDefault && <Badge tone="neutral">Default project</Badge>}
            {project?.createdAt && <span>Created {formatDate(project.createdAt)}</span>}
          </div>
          <p className="text-xs text-ink-subtle">Project details cannot be edited after creation.</p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>All projects in {organization?.name}</CardTitle>
        </CardHeader>
        <CardContent>
          <ul className="flex flex-col divide-y divide-border">
            {projects.map((p) => (
              <li key={p.id} className="flex items-center justify-between py-2.5 text-sm">
                <span className="text-ink">{p.name}</span>
                {p.isDefault && <Badge tone="neutral">Default</Badge>}
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}
