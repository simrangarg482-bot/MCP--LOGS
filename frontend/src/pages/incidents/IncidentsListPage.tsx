import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { FilterBar } from "@/components/data/FilterBar";
import { Select } from "@/components/ui/Select";
import { Input } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { DataTable, type DataTableColumn } from "@/components/data/DataTable";
import { SeverityBadge } from "@/components/data/SeverityBadge";
import { StatusBadge } from "@/components/data/StatusBadge";
import { useDebounce } from "@/hooks/useDebounce";
import { listIncidents } from "@/api/incidents";
import type { Incident, IncidentSeverity, IncidentStatus } from "@/types/incident";
import { formatDateTime, formatRelativeTime } from "@/utils/date";

const SEVERITIES: IncidentSeverity[] = ["critical", "high", "medium", "low"];
const STATUSES: IncidentStatus[] = ["open", "investigating", "resolved", "closed"];
const PAGE_SIZE = 20;

// Real filters only -- the backend's `IncidentFilter` (app/core/incidents/
// schemas.py) takes a single severity/status/owner_team value each, never
// arrays, has no free-text `search` field, and exposes no total-count query
// -- see `listIncidents`'s own docstring in api/incidents.ts for why this
// page uses "load more" (offset/limit) instead of page-number pagination.
export function IncidentsListPage() {
  const navigate = useNavigate();
  const [severity, setSeverity] = useState<IncidentSeverity | "">("");
  const [status, setStatus] = useState<IncidentStatus | "">("");
  const [ownerTeamInput, setOwnerTeamInput] = useState("");
  const ownerTeam = useDebounce(ownerTeamInput, 300);
  const [limit, setLimit] = useState(PAGE_SIZE);

  const filters = {
    severity: severity || undefined,
    status: status || undefined,
    ownerTeam: ownerTeam || undefined,
    limit,
    offset: 0,
  };

  const incidentsQuery = useQuery({
    queryKey: ["incidents", filters],
    queryFn: () => listIncidents(filters),
  });

  const activeFilterCount = [severity, status, ownerTeam].filter(Boolean).length;

  function handleClearFilters() {
    setSeverity("");
    setStatus("");
    setOwnerTeamInput("");
    setLimit(PAGE_SIZE);
  }

  const columns: DataTableColumn<Incident>[] = [
    {
      key: "id",
      header: "ID",
      render: (row) => <span className="font-mono text-xs text-ink-muted">{row.id.slice(0, 8)}</span>,
    },
    {
      key: "title",
      header: "Title",
      render: (row) => <span className="max-w-xs truncate text-ink">{row.title}</span>,
      className: "max-w-xs",
    },
    { key: "severity", header: "Severity", render: (row) => <SeverityBadge severity={row.severity} /> },
    { key: "status", header: "Status", render: (row) => <StatusBadge status={row.status} /> },
    {
      key: "ownerTeam",
      header: "Owner team",
      render: (row) => row.ownerTeam ?? <span className="text-ink-subtle">Unassigned</span>,
    },
    {
      key: "createdAt",
      header: "Created",
      render: (row) => (
        <span title={formatDateTime(row.createdAt)} className="text-ink-muted">
          {formatRelativeTime(row.createdAt)}
        </span>
      ),
    },
    {
      key: "updatedAt",
      header: "Updated",
      render: (row) => (
        <span title={formatDateTime(row.updatedAt)} className="text-ink-muted">
          {formatRelativeTime(row.updatedAt)}
        </span>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Incidents"
        description="Track and investigate active and historical incidents."
        actions={
          <Button variant="primary" className="gap-1.5" onClick={() => navigate("/incidents/new")}>
            <Plus className="h-4 w-4" />
            New incident
          </Button>
        }
      />

      <div className="flex flex-col gap-3">
        <FilterBar activeCount={activeFilterCount} onClear={handleClearFilters}>
          <Select
            value={severity}
            onChange={(e) => setSeverity(e.target.value as IncidentSeverity | "")}
            className="w-40"
          >
            <option value="">All severities</option>
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {s[0].toUpperCase() + s.slice(1)}
              </option>
            ))}
          </Select>

          <Select value={status} onChange={(e) => setStatus(e.target.value as IncidentStatus | "")} className="w-40">
            <option value="">All statuses</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s[0].toUpperCase() + s.slice(1)}
              </option>
            ))}
          </Select>

          <Input
            value={ownerTeamInput}
            onChange={(e) => setOwnerTeamInput(e.target.value)}
            placeholder="Filter by owner team…"
            className="w-48"
          />
        </FilterBar>
      </div>

      <Card>
        <DataTable
          columns={columns}
          rows={incidentsQuery.data ?? []}
          rowKey={(row) => row.id}
          isLoading={incidentsQuery.isLoading}
          isError={incidentsQuery.isError}
          onRetry={() => incidentsQuery.refetch()}
          onRowClick={(row) => navigate(`/incidents/${row.id}`)}
          emptyTitle="No incidents found"
          emptyDescription="Try adjusting your filters, or create a new incident."
        />
        {incidentsQuery.data && incidentsQuery.data.length >= limit && (
          <div className="flex justify-center border-t border-border py-3">
            <Button variant="secondary" size="sm" onClick={() => setLimit((l) => l + PAGE_SIZE)}>
              Load more
            </Button>
          </div>
        )}
      </Card>
    </div>
  );
}
