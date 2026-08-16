import { useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { Sparkles, Link2 } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { SeverityBadge } from "@/components/data/SeverityBadge";
import { StatusBadge } from "@/components/data/StatusBadge";
import { Tabs } from "@/components/ui/Tabs";
import { Card, CardContent } from "@/components/ui/Card";
import { LoadingState } from "@/components/ui/LoadingState";
import { ErrorState } from "@/components/ui/ErrorState";
import { EmptyState } from "@/components/ui/EmptyState";
import { Button } from "@/components/ui/Button";
import { Timeline } from "@/components/domain/Timeline";
import { InvestigationPanel } from "@/components/domain/InvestigationPanel";
import { PostmortemPanel } from "@/components/domain/PostmortemPanel";
import { getIncident, getIncidentTimeline, addIncidentNote } from "@/api/incidents";
import { investigateIncident, searchSimilarIncidents } from "@/api/ask";
import { useAuth } from "@/context/AuthContext";
import { formatDateTime, formatRelativeTime } from "@/utils/date";

const TABS = [
  { key: "timeline", label: "Timeline" },
  { key: "investigation", label: "AI Investigation" },
  { key: "related", label: "Related Evidence" },
  { key: "postmortem", label: "Postmortem" },
];

export function IncidentDetailPage() {
  const { id = "" } = useParams();
  const { user } = useAuth();
  // Mirrors the real gate `core.incidents.service.add_timeline_note`
  // enforces -- UX only, the backend re-checks regardless.
  const canWrite = Boolean(user?.permissions.includes("incident:write"));
  const [activeTab, setActiveTab] = useState("timeline");
  const [note, setNote] = useState("");
  const queryClient = useQueryClient();

  const incidentQuery = useQuery({ queryKey: ["incident", id], queryFn: () => getIncident(id) });
  const timelineQuery = useQuery({ queryKey: ["incident", id, "timeline"], queryFn: () => getIncidentTimeline(id) });

  // `POST /incidents/{id}/investigate` -- real contract, not the fictional
  // GET `AiInvestigation` this page used to call. A mutation, not a query:
  // triage_incident always investigates fresh (AGENT_WORKFLOWS.md section
  // 11.3), it isn't cached, idempotent, read-only data.
  const investigateMutation = useMutation({
    mutationFn: () => investigateIncident(id),
    onSuccess: () => {
      // The backend best-effort attaches the result to the incident's
      // timeline (`core.incidents.service.record_investigation_result`) --
      // refetching lets the Timeline tab show it too.
      queryClient.invalidateQueries({ queryKey: ["incident", id, "timeline"] });
    },
  });

  const relatedEvidenceQuery = useQuery({
    queryKey: ["incident", id, "related-evidence"],
    queryFn: () => searchSimilarIncidents(incidentQuery.data!.description, 10),
    enabled: activeTab === "related" && Boolean(incidentQuery.data),
  });

  const addNoteMutation = useMutation({
    mutationFn: (body: string) => addIncidentNote(id, body),
    onSuccess: () => {
      setNote("");
      queryClient.invalidateQueries({ queryKey: ["incident", id, "timeline"] });
    },
  });

  if (incidentQuery.isLoading) return <LoadingState label="Loading incident…" />;
  if (incidentQuery.isError || !incidentQuery.data) {
    return <ErrorState onRetry={() => incidentQuery.refetch()} />;
  }

  const incident = incidentQuery.data;

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        breadcrumbs={[{ label: "Incidents", path: "/incidents" }, { label: incident.id.slice(0, 8) }]}
        title={incident.title}
        description={incident.description}
        actions={
          <>
            <SeverityBadge severity={incident.severity} />
            <StatusBadge status={incident.status} />
          </>
        }
      />

      <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-ink-muted">
        <span>
          Owner team: <span className="font-medium text-ink">{incident.ownerTeam ?? "Unassigned"}</span>
        </span>
        <span title={formatDateTime(incident.createdAt)}>
          Created: <span className="font-medium text-ink">{formatRelativeTime(incident.createdAt)}</span>
        </span>
        {incident.resolvedAt && (
          <span title={formatDateTime(incident.resolvedAt)}>
            Resolved: <span className="font-medium text-ink">{formatRelativeTime(incident.resolvedAt)}</span>
          </span>
        )}
      </div>

      <Tabs items={TABS} activeKey={activeTab} onChange={setActiveTab} />

      {activeTab === "timeline" && (
        <Card>
          <CardContent className="flex flex-col gap-4">
            {timelineQuery.isLoading && <LoadingState label="Loading timeline…" />}
            {timelineQuery.isError && <ErrorState onRetry={() => timelineQuery.refetch()} />}
            {timelineQuery.data && <Timeline entries={timelineQuery.data} />}

            {canWrite ? (
              <form
                className="flex gap-2 border-t border-border pt-4"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (note.trim()) addNoteMutation.mutate(note.trim());
                }}
              >
                <label className="flex-1">
                  <span className="sr-only">Add a note to the timeline</span>
                  <input
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    placeholder="Add a note to the timeline…"
                    className="h-9 w-full rounded-md border border-border bg-white px-3 text-sm text-ink placeholder:text-ink-subtle focus-visible:border-accent"
                  />
                </label>
                <Button type="submit" size="sm" isLoading={addNoteMutation.isPending}>
                  Post
                </Button>
              </form>
            ) : (
              <p className="border-t border-border pt-4 text-xs text-ink-muted">
                You need the <span className="font-medium text-ink">incident:write</span> permission to add timeline
                notes.
              </p>
            )}
            {addNoteMutation.isError && (
              <p role="alert" className="text-xs text-critical">Failed to post note. Please try again.</p>
            )}
          </CardContent>
        </Card>
      )}

      {activeTab === "investigation" && (
        <div className="flex flex-col gap-3">
          {investigateMutation.isIdle && (
            <Card>
              <CardContent className="flex flex-col items-center gap-3 py-10 text-center">
                <Sparkles className="h-6 w-6 text-accent" />
                <p className="text-sm text-ink-muted">
                  Run the Investigation Agent to gather verified evidence and generate root-cause hypotheses for
                  this incident.
                </p>
                <Button variant="primary" onClick={() => investigateMutation.mutate()}>
                  Start investigation
                </Button>
              </CardContent>
            </Card>
          )}

          {investigateMutation.isPending && <LoadingState label="Running investigation agents…" />}

          {investigateMutation.isError && (
            <Card>
              <CardContent className="flex flex-col items-center gap-3 py-8 text-center">
                <p role="alert" className="text-sm text-critical">The investigation failed to complete.</p>
                <Button variant="secondary" onClick={() => investigateMutation.mutate()}>
                  Retry
                </Button>
              </CardContent>
            </Card>
          )}

          {investigateMutation.isSuccess && investigateMutation.data.investigation && (
            <InvestigationPanel investigation={investigateMutation.data.investigation} />
          )}

          {investigateMutation.isSuccess && (
            <Button variant="secondary" size="sm" onClick={() => investigateMutation.mutate()} className="self-start">
              Run again
            </Button>
          )}
        </div>
      )}

      {activeTab === "related" && (
        <Card>
          <CardContent>
            {relatedEvidenceQuery.isLoading && <LoadingState label="Searching the knowledge base…" />}
            {relatedEvidenceQuery.isError && <ErrorState onRetry={() => relatedEvidenceQuery.refetch()} />}
            {relatedEvidenceQuery.data && relatedEvidenceQuery.data.length === 0 && (
              <EmptyState icon={Link2} title="No related evidence found" />
            )}
            {relatedEvidenceQuery.data && relatedEvidenceQuery.data.length > 0 && (
              <ul className="flex flex-col gap-2">
                {relatedEvidenceQuery.data.map((chunk) => (
                  <li key={chunk.chunkId}>
                    <a
                      href={chunk.sourceUrl ?? "#"}
                      target={chunk.sourceUrl ? "_blank" : undefined}
                      rel="noreferrer"
                      className="flex flex-col gap-1 rounded-md border border-border bg-white px-3 py-2.5 hover:border-accent-border hover:bg-accent-subtle"
                    >
                      <div className="flex items-center gap-2">
                        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-ink-muted">
                          {chunk.collection}
                        </span>
                        <span className="truncate text-sm font-medium text-ink">{chunk.title ?? "Untitled"}</span>
                      </div>
                      <p className="line-clamp-2 text-xs text-ink-muted">{chunk.content}</p>
                    </a>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      )}

      {activeTab === "postmortem" && <PostmortemPanel incident={incident} />}
    </div>
  );
}
