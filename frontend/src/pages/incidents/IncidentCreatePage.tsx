import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Button } from "@/components/ui/Button";
import { createIncident } from "@/api/incidents";
import type { IncidentSeverity } from "@/types/incident";
import { useToast } from "@/context/ToastContext";
import { useAuth } from "@/context/AuthContext";

const SEVERITIES: IncidentSeverity[] = ["critical", "high", "medium", "low"];

export function IncidentCreatePage() {
  const navigate = useNavigate();
  const { toast } = useToast();
  const { user } = useAuth();
  // Mirrors the real gate `core.incidents.service.create_incident` enforces
  // -- UX only, the backend re-checks regardless.
  const canWrite = Boolean(user?.permissions.includes("incident:write"));
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [severity, setSeverity] = useState<IncidentSeverity>("medium");

  const createMutation = useMutation({
    mutationFn: () => createIncident({ title, description, severity }),
    onSuccess: (incident) => {
      toast({ variant: "success", title: "Incident created" });
      navigate(`/incidents/${incident.id}`);
    },
    onError: () => {
      toast({ variant: "error", title: "Failed to create incident" });
    },
  });

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        breadcrumbs={[{ label: "Incidents", path: "/incidents" }, { label: "New" }]}
        title="New incident"
        description="Open a new incident record for the team to track and investigate."
      />

      <Card>
        <CardContent>
          {!canWrite && (
            <p className="mb-4 rounded-md border border-border bg-slate-50 px-3 py-2 text-xs text-ink-muted">
              You need the <span className="font-medium text-ink">incident:write</span> permission to create an
              incident.
            </p>
          )}
          <form
            className="flex flex-col gap-4"
            onSubmit={(event) => {
              event.preventDefault();
              createMutation.mutate();
            }}
          >
            <label className="flex flex-col gap-1 text-xs font-medium text-ink-muted">
              Title
              <Input
                required
                disabled={!canWrite}
                placeholder="e.g. Payment API returning 500 errors"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
              />
            </label>

            <label className="flex flex-col gap-1 text-xs font-medium text-ink-muted">
              Description
              <textarea
                required
                disabled={!canWrite}
                rows={5}
                placeholder="What's happening, and what's the customer/system impact so far?"
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                className="w-full rounded-md border border-border bg-white px-3 py-2 text-sm text-ink placeholder:text-ink-subtle focus-visible:border-accent disabled:bg-slate-50 disabled:text-ink-subtle"
              />
            </label>

            <label className="flex flex-col gap-1 text-xs font-medium text-ink-muted">
              Severity
              <Select
                value={severity}
                disabled={!canWrite}
                onChange={(event) => setSeverity(event.target.value as IncidentSeverity)}
                className="w-48"
              >
                {SEVERITIES.map((s) => (
                  <option key={s} value={s}>
                    {s[0].toUpperCase() + s.slice(1)}
                  </option>
                ))}
              </Select>
            </label>

            <div className="mt-2 flex justify-end gap-2">
              <Button type="button" variant="secondary" onClick={() => navigate("/incidents")}>
                Cancel
              </Button>
              <Button
                type="submit"
                variant="primary"
                disabled={createMutation.isPending || !title || !description || !canWrite}
              >
                {createMutation.isPending ? "Creating…" : "Create incident"}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
