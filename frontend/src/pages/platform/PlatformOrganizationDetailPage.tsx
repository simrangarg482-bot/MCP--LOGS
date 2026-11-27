import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { getPlatformOrganization, getPlatformStatusHistory } from "@/api/platform";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorState } from "@/components/ui/ErrorState";
import { LoadingState } from "@/components/ui/LoadingState";
import { PageHeader } from "@/components/layout/PageHeader";
import { useToast } from "@/context/ToastContext";
import { usePlatformAuth } from "@/context/PlatformAuthContext";
import type { ApiError } from "@/types/common";
import type { LifecycleAction, StatusChangeResult } from "@/types/platform";
import { allowedActions, statusLabel, statusTone } from "@/utils/platform";
import { LifecycleDialog } from "./LifecycleDialog";

const ACTION_LABEL: Record<LifecycleAction, string> = {
  activate: "Activate",
  suspend: "Suspend",
  reactivate: "Reactivate",
};

export function PlatformOrganizationDetailPage() {
  const { id = "" } = useParams();
  const { operator } = usePlatformAuth();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [dialog, setDialog] = useState<LifecycleAction | null>(null);

  const org = useQuery({
    queryKey: ["platform", "organization", id],
    queryFn: () => getPlatformOrganization(id),
    retry: false,
  });
  const history = useQuery({
    queryKey: ["platform", "organization", id, "history"],
    queryFn: () => getPlatformStatusHistory(id),
    retry: false,
  });

  function handleDone(result: StatusChangeResult) {
    setDialog(null);
    queryClient.setQueryData(["platform", "organization", id], result.organization);
    void queryClient.invalidateQueries({ queryKey: ["platform", "organization", id, "history"] });
    void queryClient.invalidateQueries({ queryKey: ["platform", "organizations"] });
    toast({
      variant: "success",
      title: `Organization ${result.newStatus === "suspended" ? "suspended" : result.previousStatus === "suspended" ? "reactivated" : "activated"}`,
      description: result.notificationQueued ? "Its administrators are being notified by email." : undefined,
    });
  }

  if (org.isLoading) return <LoadingState label="Loading organization…" />;
  if (org.isError || !org.data) {
    const notFound = (org.error as ApiError | null)?.status === 404;
    return (
      <ErrorState
        title={notFound ? "Organization not found" : "Could not load this organization"}
        description={notFound ? "It may have been deleted." : undefined}
        onRetry={notFound ? undefined : () => void org.refetch()}
      />
    );
  }

  const data = org.data;
  const canChange = operator?.role === "admin";

  return (
    <>
      <PageHeader
        title={data.name}
        description={data.slug}
        breadcrumbs={[{ label: "Organizations", path: "/platform" }, { label: data.name }]}
        actions={
          canChange ? (
            allowedActions(data.status).map((action) => (
              <Button
                key={action}
                variant={action === "suspend" ? "danger" : "primary"}
                onClick={() => setDialog(action)}
              >
                {ACTION_LABEL[action]}
              </Button>
            ))
          ) : (
            <span className="text-xs text-ink-subtle">Read-only: administrators change status.</span>
          )
        }
      />

      <Card>
        <CardHeader>
          <CardTitle>Details</CardTitle>
          <Badge tone={statusTone(data.status)} data-testid="org-status">
            {statusLabel(data.status)}
          </Badge>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-1 gap-4 text-sm sm:grid-cols-2">
            <Item label="Organization ID" value={<span className="font-mono text-xs">{data.id}</span>} />
            <Item label="Members" value={data.memberCount} />
            <Item label="Created" value={new Date(data.createdAt).toLocaleString()} />
            <Item
              label="Status last changed"
              value={data.statusChangedAt ? new Date(data.statusChangedAt).toLocaleString() : "Never"}
            />
            {data.status === "suspended" && (
              <Item label="Message shown to the customer" value={data.statusMessagePublic ?? "None"} />
            )}
          </dl>
          <p className="mt-4 text-xs text-ink-subtle">
            <Link to="/platform" className="hover:underline">
              Back to all organizations
            </Link>
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Status history</CardTitle>
          <span className="text-xs text-ink-subtle">Visible to platform operators only</span>
        </CardHeader>
        {history.isLoading ? (
          <LoadingState label="Loading history…" />
        ) : history.isError ? (
          <ErrorState title="Could not load the history" onRetry={() => void history.refetch()} />
        ) : history.data && history.data.items.length === 0 ? (
          <EmptyState title="No status changes yet" description="Organizations created through signup start active; others start onboarding." />
        ) : (
          <ol className="divide-y divide-white/5" data-testid="status-history">
            {history.data?.items.map((entry) => (
              <li key={entry.id} className="px-5 py-4 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={statusTone(entry.previousStatus)}>{statusLabel(entry.previousStatus)}</Badge>
                  <span aria-hidden="true">→</span>
                  <Badge tone={statusTone(entry.newStatus)}>{statusLabel(entry.newStatus)}</Badge>
                  <span className="text-xs text-ink-subtle">
                    {new Date(entry.changedAt).toLocaleString()} · {entry.operatorEmail ?? entry.operatorUserId}
                  </span>
                </div>
                <p className="mt-2 text-ink">
                  <span className="text-xs text-ink-subtle">Reason: </span>
                  {entry.reason}
                </p>
                {entry.customerMessage && (
                  <p className="mt-1 text-ink-muted">
                    <span className="text-xs text-ink-subtle">Shown to customer: </span>
                    {entry.customerMessage}
                  </p>
                )}
                {entry.internalNote && (
                  <p className="mt-1 text-ink-muted">
                    <span className="text-xs text-ink-subtle">Internal note: </span>
                    {entry.internalNote}
                  </p>
                )}
              </li>
            ))}
          </ol>
        )}
      </Card>

      {dialog && (
        <LifecycleDialog action={dialog} organization={data} onClose={() => setDialog(null)} onDone={handleDone} />
      )}
    </>
  );
}

function Item({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-ink-subtle">{label}</dt>
      <dd className="mt-0.5 text-ink">{value}</dd>
    </div>
  );
}
