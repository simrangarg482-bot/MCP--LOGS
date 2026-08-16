import { MessageSquare, Sparkles } from "lucide-react";
import type { TimelineEntry, TimelineInvestigationEventData, TimelineNoteEventData } from "@/types/incident";
import { formatDateTime, formatRelativeTime } from "@/utils/date";

function isInvestigationEntry(
  entry: TimelineEntry,
): entry is TimelineEntry & { eventData: TimelineInvestigationEventData } {
  return entry.eventType === "investigation";
}

function isNoteEntry(entry: TimelineEntry): entry is TimelineEntry & { eventData: TimelineNoteEventData } {
  return entry.eventType === "note";
}

export function Timeline({ entries }: { entries: TimelineEntry[] }) {
  if (entries.length === 0) {
    return <p className="text-sm text-ink-muted">No timeline events yet.</p>;
  }

  return (
    <ol className="relative flex flex-col gap-5 border-l border-border pl-5">
      {entries.map((entry) => {
        const Icon = entry.eventType === "investigation" ? Sparkles : MessageSquare;
        return (
          <li key={entry.id} className="relative">
            <span className="absolute -left-[1.6rem] flex h-5 w-5 items-center justify-center rounded-full border border-border bg-white">
              <Icon className="h-3 w-3 text-ink-muted" />
            </span>
            <div className="flex flex-wrap items-baseline gap-2">
              <span className="text-sm font-medium text-ink">{entry.actor}</span>
              <time className="text-xs text-ink-subtle" title={formatDateTime(entry.occurredAt)}>
                {formatRelativeTime(entry.occurredAt)}
              </time>
            </div>

            {isNoteEntry(entry) && <p className="mt-0.5 text-sm text-ink-muted">{entry.eventData.note}</p>}

            {isInvestigationEntry(entry) && (
              <div className="mt-1 flex flex-col gap-1 text-sm text-ink-muted">
                <p>
                  Investigation ran — {entry.eventData.evidence.length} verified evidence item
                  {entry.eventData.evidence.length === 1 ? "" : "s"}, {entry.eventData.hypotheses.length}{" "}
                  hypothesis{entry.eventData.hypotheses.length === 1 ? "" : "es"} (AI-generated, unverified).
                </p>
                {entry.eventData.suggestedOwnerTeam && (
                  <p className="text-xs text-ink-subtle">
                    Suggested owner: <span className="font-medium text-ink">{entry.eventData.suggestedOwnerTeam}</span>
                  </p>
                )}
                <p className="text-xs text-ink-subtle">See the "AI Investigation" tab for full detail.</p>
              </div>
            )}
          </li>
        );
      })}
    </ol>
  );
}
