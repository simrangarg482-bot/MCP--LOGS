import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { DataTable, type DataTableColumn } from "@/components/data/DataTable";
import { EmptyState } from "@/components/ui/EmptyState";
import { listAuditLog } from "@/api/audit";
import type { AuditLogEntry } from "@/types/audit";
import { useTenant } from "@/context/TenantContext";
import { useDebounce } from "@/hooks/useDebounce";
import { formatDateTime, formatRelativeTime } from "@/utils/date";

const PAGE_SIZE = 50;

export function AuditPage() {
  const { organization } = useTenant();
  const [resourceTypeInput, setResourceTypeInput] = useState("");
  const [actorInput, setActorInput] = useState("");
  const resourceType = useDebounce(resourceTypeInput, 300);
  const actor = useDebounce(actorInput, 300);
  const [limit, setLimit] = useState(PAGE_SIZE);

  const filters = {
    resourceType: resourceType || undefined,
    actor: actor || undefined,
    limit,
    offset: 0,
  };

  const auditQuery = useQuery({
    queryKey: ["audit", organization?.id, filters],
    queryFn: () => listAuditLog(organization!.id, filters),
    enabled: Boolean(organization),
  });

  const columns: DataTableColumn<AuditLogEntry>[] = [
    {
      key: "occurredAt",
      header: "Timestamp",
      render: (row) => (
        <span title={formatDateTime(row.occurredAt)} className="text-ink-muted">
          {formatRelativeTime(row.occurredAt)}
        </span>
      ),
    },
    { key: "actor", header: "Actor", render: (row) => <span className="font-mono text-xs">{row.actor}</span> },
    { key: "action", header: "Action", render: (row) => row.action },
    {
      key: "resourceType",
      header: "Resource",
      render: (row) => (
        <span>
          {row.resourceType}
          {row.resourceId && <span className="ml-1 font-mono text-xs text-ink-subtle">{row.resourceId.slice(0, 8)}</span>}
        </span>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Audit log"
        description="Every consequential action recorded for this organization, newest first."
      />

      <div className="flex flex-col gap-3 sm:flex-row">
        <Input
          value={actorInput}
          onChange={(e) => setActorInput(e.target.value)}
          placeholder="Filter by actor…"
          aria-label="Filter by actor"
          className="w-full sm:w-64"
        />
        <Input
          value={resourceTypeInput}
          onChange={(e) => setResourceTypeInput(e.target.value)}
          placeholder="Filter by resource type…"
          aria-label="Filter by resource type"
          className="w-full sm:w-64"
        />
      </div>

      <Card>
        <DataTable
          columns={columns}
          rows={auditQuery.data ?? []}
          rowKey={(row) => row.id}
          isLoading={auditQuery.isLoading}
          isError={auditQuery.isError}
          onRetry={() => auditQuery.refetch()}
          emptyTitle="No audit events found"
          emptyDescription="Try adjusting your filters."
        />
        {auditQuery.data && auditQuery.data.length >= limit && (
          <div className="flex justify-center border-t border-border py-3">
            <button
              className="text-xs font-medium text-accent hover:underline"
              onClick={() => setLimit((l) => l + PAGE_SIZE)}
            >
              Load more
            </button>
          </div>
        )}
      </Card>

      {!organization && <EmptyState title="No organization selected" />}
    </div>
  );
}
