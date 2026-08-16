import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Plus, Trash2 } from "lucide-react";
import { Card, CardContent } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { LoadingState } from "@/components/ui/LoadingState";
import {
  approvePostmortem,
  generatePostmortem,
  getPostmortemByIncident,
  updatePostmortem,
} from "@/api/postmortems";
import type { ActionItem, ActionItemStatus, Postmortem } from "@/types/postmortem";
import type { Incident } from "@/types/incident";
import type { ApiError } from "@/types/common";
import { useAuth } from "@/context/AuthContext";
import { formatRelativeTime } from "@/utils/date";

const STATUS_TONE: Record<Postmortem["status"], "neutral" | "warning" | "accent" | "success"> = {
  draft: "neutral",
  in_review: "warning",
  approved: "success",
  published: "success",
};

const ACTION_ITEM_STATUSES: ActionItemStatus[] = ["open", "in_progress", "done"];

/**
 * A postmortem is incident-scoped -- there is no standalone `GET
 * /postmortems` list endpoint on the real backend, only per-incident
 * lookup/create/update/approve (confirmed by direct inspection). This is
 * why the workflow lives as a tab on the incident detail page, not a
 * separate top-level `/postmortems` route.
 */
export function PostmortemPanel({ incident }: { incident: Incident }) {
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<{ rootCause: string; actionItems: ActionItem[] } | null>(null);

  const canWrite = Boolean(user?.permissions.includes("postmortem:write"));
  const canApprove = Boolean(user?.permissions.includes("postmortem:approve"));

  const postmortemQuery = useQuery({
    queryKey: ["incident", incident.id, "postmortem"],
    queryFn: () => getPostmortemByIncident(incident.id),
  });

  const postmortem = postmortemQuery.data ?? null;
  const editable = postmortem !== null && (postmortem.status === "draft" || postmortem.status === "in_review") && canWrite;

  useEffect(() => {
    if (postmortem) {
      setDraft({ rootCause: postmortem.rootCause ?? "", actionItems: postmortem.actionItems });
    } else {
      setDraft(null);
    }
  }, [postmortem]);

  const generateMutation = useMutation({
    mutationFn: () => generatePostmortem(incident.id),
    onSuccess: (result) => {
      queryClient.setQueryData(["incident", incident.id, "postmortem"], result);
    },
  });

  const saveMutation = useMutation({
    mutationFn: (patch: { rootCause?: string; actionItems?: ActionItem[]; status?: "draft" | "in_review" }) =>
      updatePostmortem(postmortem!.id, patch),
    onSuccess: (result) => {
      queryClient.setQueryData(["incident", incident.id, "postmortem"], result);
    },
  });

  const approveMutation = useMutation({
    mutationFn: () => approvePostmortem(postmortem!.id),
    onSuccess: (result) => {
      queryClient.setQueryData(["incident", incident.id, "postmortem"], result);
    },
  });

  if (postmortemQuery.isLoading) return <LoadingState label="Loading postmortem…" />;

  if (postmortemQuery.isError) {
    const apiError = postmortemQuery.error as unknown as ApiError;
    if (apiError?.status === 403) {
      return (
        <Card>
          <CardContent className="py-8 text-center text-sm text-ink-muted">
            A postmortem exists for this incident, but you need <span className="font-medium text-ink">postmortem:write</span> or{" "}
            <span className="font-medium text-ink">postmortem:approve</span> to view it before it's reviewed.
          </CardContent>
        </Card>
      );
    }
    return (
      <Card>
        <CardContent className="flex flex-col items-center gap-3 py-8 text-center">
          <p role="alert" className="text-sm text-critical">Failed to load the postmortem.</p>
          <Button variant="secondary" onClick={() => postmortemQuery.refetch()}>
            Retry
          </Button>
        </CardContent>
      </Card>
    );
  }

  // No postmortem exists yet -- offer to generate one, subject to the same
  // real backend precondition `trigger_postmortem_generation` enforces
  // (incident must be resolved/closed) plus `postmortem:write`.
  if (!postmortem) {
    const incidentReady = incident.status === "resolved" || incident.status === "closed";
    return (
      <Card>
        <CardContent className="flex flex-col items-center gap-3 py-10 text-center">
          <FileText className="h-6 w-6 text-accent" />
          {!incidentReady && (
            <p className="text-sm text-ink-muted">
              This incident must be resolved or closed before a postmortem can be generated.
            </p>
          )}
          {incidentReady && !canWrite && (
            <p className="text-sm text-ink-muted">You need the postmortem:write permission to generate one.</p>
          )}
          {incidentReady && canWrite && (
            <p className="text-sm text-ink-muted">No postmortem exists for this incident yet.</p>
          )}
          <Button
            variant="primary"
            disabled={!incidentReady || !canWrite || generateMutation.isPending}
            isLoading={generateMutation.isPending}
            onClick={() => generateMutation.mutate()}
          >
            Generate postmortem
          </Button>
          {generateMutation.isError && (
            <p role="alert" className="text-xs text-critical">Failed to generate a postmortem. Please try again.</p>
          )}
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <Card>
        <CardContent className="flex flex-col gap-4">
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <Badge tone={STATUS_TONE[postmortem.status]}>{postmortem.status.replace("_", " ")}</Badge>
              <span className="text-xs text-ink-subtle">Updated {formatRelativeTime(postmortem.updatedAt)}</span>
            </div>
            <div className="flex gap-2">
              {editable && postmortem.status === "draft" && (
                <Button
                  variant="secondary"
                  size="sm"
                  isLoading={saveMutation.isPending}
                  onClick={() => saveMutation.mutate({ status: "in_review" })}
                >
                  Submit for review
                </Button>
              )}
              {canApprove && (postmortem.status === "draft" || postmortem.status === "in_review") && (
                <Button variant="primary" size="sm" isLoading={approveMutation.isPending} onClick={() => approveMutation.mutate()}>
                  Approve
                </Button>
              )}
            </div>
          </div>

          <div>
            <label
              htmlFor="postmortem-root-cause"
              className="mb-1.5 block text-xs font-medium uppercase tracking-wide text-ink-subtle"
            >
              Root cause {editable && <span className="normal-case text-ink-subtle">(editable while draft/in review)</span>}
            </label>
            {editable ? (
              <textarea
                id="postmortem-root-cause"
                value={draft?.rootCause ?? ""}
                onChange={(e) => setDraft((d) => (d ? { ...d, rootCause: e.target.value } : d))}
                rows={4}
                className="w-full rounded-md border border-border bg-white px-3 py-2 text-sm text-ink focus-visible:border-accent"
              />
            ) : (
              <p className="text-sm text-ink">{postmortem.rootCause ?? "Not yet determined."}</p>
            )}
          </div>

          <div>
            <div className="mb-1.5 flex items-center justify-between">
              <p className="text-xs font-medium uppercase tracking-wide text-ink-subtle">Action items</p>
              {editable && (
                <button
                  type="button"
                  className="flex items-center gap-1 text-xs font-medium text-accent hover:underline"
                  onClick={() =>
                    setDraft((d) =>
                      d ? { ...d, actionItems: [...d.actionItems, { description: "", owner: null, status: "open" }] } : d,
                    )
                  }
                >
                  <Plus className="h-3 w-3" /> Add item
                </button>
              )}
            </div>
            {(editable ? draft?.actionItems : postmortem.actionItems)?.length === 0 && (
              <p className="text-sm text-ink-muted">No action items yet.</p>
            )}
            <ul className="flex flex-col gap-2">
              {(editable ? draft?.actionItems ?? [] : postmortem.actionItems).map((item, index) => (
                <li key={index} className="flex items-center gap-2 rounded-md border border-border bg-white px-2.5 py-2">
                  {editable ? (
                    <>
                      <input
                        value={item.description}
                        onChange={(e) =>
                          setDraft((d) =>
                            d
                              ? {
                                  ...d,
                                  actionItems: d.actionItems.map((it, i) =>
                                    i === index ? { ...it, description: e.target.value } : it,
                                  ),
                                }
                              : d,
                          )
                        }
                        placeholder="Action item description…"
                        aria-label="Action item description"
                        className="h-8 flex-1 rounded border border-border px-2 text-sm text-ink"
                      />
                      <select
                        value={item.status}
                        onChange={(e) =>
                          setDraft((d) =>
                            d
                              ? {
                                  ...d,
                                  actionItems: d.actionItems.map((it, i) =>
                                    i === index ? { ...it, status: e.target.value as ActionItemStatus } : it,
                                  ),
                                }
                              : d,
                          )
                        }
                        aria-label="Action item status"
                        className="h-8 rounded border border-border px-1.5 text-xs text-ink"
                      >
                        {ACTION_ITEM_STATUSES.map((s) => (
                          <option key={s} value={s}>
                            {s.replace("_", " ")}
                          </option>
                        ))}
                      </select>
                      <button
                        type="button"
                        onClick={() =>
                          setDraft((d) => (d ? { ...d, actionItems: d.actionItems.filter((_, i) => i !== index) } : d))
                        }
                        aria-label="Remove action item"
                        className="text-ink-subtle hover:text-critical"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </>
                  ) : (
                    <div className="flex flex-1 items-center justify-between">
                      <span className="text-sm text-ink">{item.description}</span>
                      <Badge tone="neutral">{item.status.replace("_", " ")}</Badge>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </div>

          {editable && (
            <div className="flex justify-end gap-2 border-t border-border pt-3">
              <Button
                variant="primary"
                size="sm"
                isLoading={saveMutation.isPending}
                onClick={() =>
                  draft && saveMutation.mutate({ rootCause: draft.rootCause, actionItems: draft.actionItems })
                }
              >
                Save changes
              </Button>
            </div>
          )}
          {saveMutation.isError && <p role="alert" className="text-xs text-critical">Failed to save. Please try again.</p>}
          {approveMutation.isError && <p role="alert" className="text-xs text-critical">Failed to approve. Please try again.</p>}
        </CardContent>
      </Card>
    </div>
  );
}
