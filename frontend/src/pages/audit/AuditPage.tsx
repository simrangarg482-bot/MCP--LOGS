import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { DataTable, type DataTableColumn } from "@/components/data/DataTable";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorState } from "@/components/ui/ErrorState";
import { listAuditLog } from "@/api/audit";
import type { AuditLogEntry } from "@/types/audit";
import type { ApiError } from "@/types/common";
import { useTenant } from "@/context/TenantContext";
import { useDebounce } from "@/hooks/useDebounce";
import { formatDateTime, formatRelativeTime } from "@/utils/date";
import { AUDIT_ACTION_GROUPS, auditReason, describeAuditAction } from "@/utils/scim";

const PAGE_SIZE = 50;

export function AuditPage() {
  const { organization } = useTenant();
  const [resourceTypeInput, setResourceTypeInput] = useState("");
  const [actorInput, setActorInput] = useState("");
  const resourceType = useDebounce(resourceTypeInput, 300);
  const actor = useDebounce(actorInput, 300);
  const [limit, setLimit] = useState(PAGE_SIZE);
  const [actionPrefix, setActionPrefix] = useState("");

  const filters = {
    actionPrefix: actionPrefix || undefined,
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
  const queryError = auditQuery.error as ApiError | null;

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
    {
      key: "action",
      header: "Action",
      render: (row) => {
        const description = describeAuditAction(row.action, row.eventMetadata);
        const reason = auditReason(row.eventMetadata);
        return (
          <div>
            <p className="text-ink">{description}</p>
            {description !== row.action && <p className="font-mono text-[11px] text-ink-subtle">{row.action}</p>}
            {reason && <p className="text-xs text-ink-muted">Reason: {reason}</p>}
          </div>
        );
      },
    },
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

  if (queryError?.status === 403) {
    return (
      <div className="flex flex-col gap-4">
        <PageHeader title="Audit log" />
        <ErrorState
          title="You don't have access to the audit log"
          description="Viewing audit events requires the audit:read permission."
        />
      </div>
    );
  }

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
        <Select
          value={actionPrefix}
          onChange={(e) => {
            setActionPrefix(e.target.value);
            setLimit(PAGE_SIZE);
          }}
          aria-label="Filter by event type"
          className="w-full sm:w-56"
        >
          {AUDIT_ACTION_GROUPS.map((group) => (
            <option key={group.value} value={group.value}>
              {group.label}
            </option>
          ))}
        </Select>
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
