import { ExternalLink, FileText } from "lucide-react";
import type { Citation } from "@/types/ask";
import { cn } from "@/utils/cn";

/** A short, human-readable label for where a citation came from. */
function describeSource(sourceUrl: string | null): { label: string; host: string | null } {
  if (!sourceUrl) return { label: "Evidence excerpt", host: null };
  try {
    const url = new URL(sourceUrl);
    const segments = url.pathname.split("/").filter(Boolean);
    if (url.hostname.includes("github.com") && segments.length >= 2) {
      const repo = `${segments[0]}/${segments[1]}`;
      const filePath = segments.slice(4).join("/"); // .../blob/<ref>/<path>
      return { label: filePath ? `${repo} · ${filePath}` : repo, host: "github.com" };
    }
    const last = segments[segments.length - 1];
    return { label: last ? decodeURIComponent(last) : url.hostname, host: url.hostname };
  } catch {
    return { label: "Evidence excerpt", host: null };
  }
}

export function AskCitationList({
  citations,
  onPreview,
  activeChunkId,
}: {
  citations: Citation[];
  onPreview: (citation: Citation, index: number) => void;
  activeChunkId?: string | null;
}) {
  if (citations.length === 0) return null;

  return (
    <div>
      <p className="text-xs font-medium uppercase tracking-wide text-ink-subtle">Sources</p>
      <p className="mb-2 mt-0.5 text-xs text-ink-subtle">
        {citations.length} grounded {citations.length === 1 ? "excerpt" : "excerpts"} · click a number to inspect
      </p>
      <ul className="grid gap-1.5 lg:grid-cols-2">
        {citations.map((citation, index) => {
          const { label, host } = describeSource(citation.sourceUrl);
          const isActive = activeChunkId != null && activeChunkId === citation.chunkId;
          return (
            <li key={citation.chunkId}>
              <button
                type="button"
                onClick={() => onPreview(citation, index)}
                className={cn(
                  "group flex w-full items-start gap-3 rounded-lg border px-3 py-2.5 text-left text-xs text-ink transition",
                  isActive
                    ? "border-accent-border bg-accent-subtle ring-1 ring-accent-border"
                    : "border-border bg-slate-50/70 hover:border-accent-border hover:bg-accent-subtle",
                )}
              >
                <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-white text-[11px] font-semibold text-accent shadow-subtle ring-1 ring-inset ring-border">
                  {index + 1}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="mb-1 flex items-center gap-1.5 font-medium text-ink">
                    <FileText className="h-3.5 w-3.5 shrink-0 text-ink-subtle" />
                    <span className="truncate">{label}</span>
                    {host && (
                      <span className="shrink-0 rounded bg-white px-1 py-px text-[10px] font-normal text-ink-subtle ring-1 ring-inset ring-border">
                        {host}
                      </span>
                    )}
                  </span>
                  <span className="line-clamp-2 leading-5 text-ink-muted">{citation.excerpt}</span>
                </span>
                <ExternalLink className="mt-1 h-3.5 w-3.5 shrink-0 text-ink-subtle group-hover:text-accent" />
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
