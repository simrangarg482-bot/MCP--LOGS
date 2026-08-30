import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { AlertCircle, ShieldAlert, CheckCircle2, BookOpen, Plug, Bot, ArrowRight, MessageCircleQuestion, Plus } from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from "recharts";
import { PageHeader } from "@/components/layout/PageHeader";
import { MetricCard } from "@/components/data/MetricCard";
import { DataTable, type DataTableColumn } from "@/components/data/DataTable";
import { SeverityBadge } from "@/components/data/SeverityBadge";
import { StatusBadge } from "@/components/data/StatusBadge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { LoadingState } from "@/components/ui/LoadingState";
import { ErrorState } from "@/components/ui/ErrorState";
import { Button } from "@/components/ui/Button";
import { listIncidents } from "@/api/incidents";
import { listConnectors } from "@/api/connectors";
import { listAgentStats } from "@/api/agents";
import { listKnowledgeDocuments } from "@/api/knowledge";
import type { Incident } from "@/types/incident";
import { formatRelativeTime } from "@/utils/date";

const SEVERITY_COLORS = {
  critical: "#DC2626",
  high: "#D97706",
  medium: "#64748B",
  low: "#93C5FD",
};

// A wider fetch than the 6-row table below needs, specifically so the
// severity/owner-team breakdown charts reflect a real (if capped, not
// paginated-away) sample of actual incidents rather than just the 6 most
// recent -- still real, client-computed data, not the fictional
// `getAnalyticsSummary()` this page used to call (no backend endpoint for
// it exists at all; see docs/ENGINEERING_DECISIONS.md-style audit notes in
// this session's history).
const BREAKDOWN_SAMPLE_SIZE = 100;

export function DashboardPage() {
  const navigate = useNavigate();

  const recentIncidentsQuery = useQuery({
    queryKey: ["incidents", "dashboard", "recent"],
    queryFn: () => listIncidents({ limit: 6, offset: 0 }),
  });
  const breakdownIncidentsQuery = useQuery({
    queryKey: ["incidents", "dashboard", "breakdown"],
    queryFn: () => listIncidents({ limit: BREAKDOWN_SAMPLE_SIZE, offset: 0 }),
  });
  const connectorsQuery = useQuery({ queryKey: ["connectors"], queryFn: listConnectors });
  const agentsQuery = useQuery({ queryKey: ["agents", "stats"], queryFn: listAgentStats });
  const knowledgeQuery = useQuery({
    queryKey: ["knowledge", "dashboard"],
    queryFn: () => listKnowledgeDocuments({ page: 1, pageSize: 1 }),
  });

  const incidents = recentIncidentsQuery.data ?? [];
  const breakdownIncidents = breakdownIncidentsQuery.data ?? [];
  const openCount = breakdownIncidents.filter((i) => i.status === "open" || i.status === "investigating").length;
  const criticalCount = breakdownIncidents.filter((i) => i.severity === "critical").length;
  const resolvedCount = breakdownIncidents.filter((i) => i.status === "resolved" || i.status === "closed").length;
  const connectedSources = connectorsQuery.data?.filter((connector) => connector.status === "active").length ?? 0;
  const totalSources = connectorsQuery.data?.filter((connector) => connector.status !== "disconnected").length ?? 0;

  const severityBreakdown = (["critical", "high", "medium", "low"] as const)
    .map((severity) => ({
      severity,
      count: breakdownIncidents.filter((i) => i.severity === severity).length,
    }))
    .filter((entry) => entry.count > 0);

  const ownerTeamBreakdown = Object.entries(
    breakdownIncidents.reduce<Record<string, number>>((acc, incident) => {
      const team = incident.ownerTeam ?? "Unassigned";
      acc[team] = (acc[team] ?? 0) + 1;
      return acc;
    }, {}),
  )
    .map(([ownerTeam, count]) => ({ ownerTeam, count }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 8);

  const columns: DataTableColumn<Incident>[] = [
    {
      key: "id",
      header: "Incident",
      render: (row) => <span className="font-mono text-xs font-medium text-ink">{row.id.slice(0, 8)}</span>,
    },
    { key: "severity", header: "Severity", render: (row) => <SeverityBadge severity={row.severity} /> },
    { key: "status", header: "Status", render: (row) => <StatusBadge status={row.status} /> },
    { key: "ownerTeam", header: "Owner team", render: (row) => row.ownerTeam ?? "Unassigned" },
    {
      key: "createdAt",
      header: "Created",
      render: (row) => <span className="text-ink-muted">{formatRelativeTime(row.createdAt)}</span>,
    },
  ];

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Dashboard"
        description="Current state of the engineering environment across incidents, knowledge, and agents."
        actions={<Button variant="primary" onClick={() => navigate("/ask")}><MessageCircleQuestion className="h-4 w-4" />Ask EKIP</Button>}
      />

      <div className="grid gap-3 lg:grid-cols-[1fr_auto]">
        <div className="rounded-xl border border-accent-border bg-gradient-to-r from-accent-subtle to-white px-5 py-4">
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div><p className="text-sm font-semibold text-ink">Knowledge readiness</p><p className="mt-1 text-xs text-ink-muted">{connectedSources} of {totalSources} configured sources active · {knowledgeQuery.data?.total ?? 0} searchable documents</p></div>
            <Button size="sm" variant="secondary" onClick={() => navigate("/connectors")}>Review sources<ArrowRight className="h-3.5 w-3.5" /></Button>
          </div>
        </div>
        <Button variant="secondary" className="h-full min-h-16 justify-start px-5 lg:min-w-48" onClick={() => navigate("/incidents/new")}><Plus className="h-4 w-4 text-accent" /><span className="text-left"><span className="block text-xs font-semibold">Create incident</span><span className="block text-[11px] font-normal text-ink-muted">Start a response</span></span></Button>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <MetricCard label="Open Incidents" value={openCount} icon={AlertCircle} tone="neutral" />
        <MetricCard label="Critical Incidents" value={criticalCount} icon={ShieldAlert} tone="critical" />
        <MetricCard label="Resolved Incidents" value={resolvedCount} icon={CheckCircle2} tone="success" />
        <MetricCard
          label="Knowledge Documents"
          value={knowledgeQuery.data?.total ?? "—"}
          icon={BookOpen}
        />
        <MetricCard
          label="Connected Sources"
          value={connectorsQuery.data?.filter((c) => c.status === "active").length ?? "—"}
          icon={Plug}
        />
        <MetricCard
          label="Agents with activity"
          value={agentsQuery.data?.length ?? "—"}
          icon={Bot}
        />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Severity distribution</CardTitle>
          </CardHeader>
          <CardContent className="h-64">
            {breakdownIncidentsQuery.isLoading ? (
              <LoadingState label="Loading incidents…" />
            ) : breakdownIncidentsQuery.isError ? (
              <ErrorState onRetry={() => breakdownIncidentsQuery.refetch()} />
            ) : severityBreakdown.length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={severityBreakdown}
                    dataKey="count"
                    nameKey="severity"
                    innerRadius={50}
                    outerRadius={80}
                    paddingAngle={2}
                    isAnimationActive={false}
                  >
                    {severityBreakdown.map((entry) => (
                      <Cell key={entry.severity} fill={SEVERITY_COLORS[entry.severity]} />
                    ))}
                  </Pie>
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <RechartsTooltip contentStyle={{ fontSize: 12, borderRadius: 6, borderColor: "#E2E8F0" }} />
                </PieChart>
              </ResponsiveContainer>
            ) : (
              <p className="flex h-full items-center justify-center text-sm text-ink-muted">No incidents yet.</p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Incidents by owner team</CardTitle>
          </CardHeader>
          <CardContent className="h-64">
            {breakdownIncidentsQuery.isLoading ? (
              <LoadingState label="Loading incidents…" />
            ) : breakdownIncidentsQuery.isError ? (
              <ErrorState onRetry={() => breakdownIncidentsQuery.refetch()} />
            ) : ownerTeamBreakdown.length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={ownerTeamBreakdown} margin={{ left: -20, right: 8 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#E2E8F0" vertical={false} />
                  <XAxis dataKey="ownerTeam" tick={{ fontSize: 11, fill: "#64748B" }} axisLine={false} tickLine={false} />
                  <YAxis tick={{ fontSize: 11, fill: "#64748B" }} axisLine={false} tickLine={false} width={28} />
                  <RechartsTooltip contentStyle={{ fontSize: 12, borderRadius: 6, borderColor: "#E2E8F0" }} />
                  <Bar dataKey="count" name="Incidents" fill="#2563EB" radius={[3, 3, 0, 0]} isAnimationActive={false} />
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <p className="flex h-full items-center justify-center text-sm text-ink-muted">No incidents yet.</p>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Recent incidents</CardTitle>
        </CardHeader>
        <DataTable
          columns={columns}
          rows={incidents}
          rowKey={(row) => row.id}
          isLoading={recentIncidentsQuery.isLoading}
          isError={recentIncidentsQuery.isError}
          onRetry={() => recentIncidentsQuery.refetch()}
          onRowClick={(row) => navigate(`/incidents/${row.id}`)}
        />
      </Card>
    </div>
  );
}
