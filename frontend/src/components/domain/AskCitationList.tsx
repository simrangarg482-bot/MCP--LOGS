import { useState } from "react";
import { ExternalLink, FileText } from "lucide-react";
import type { Citation } from "@/types/ask";
import { EvidencePreviewModal } from "./EvidencePreviewModal";

export function AskCitationList({ citations }: { citations: Citation[] }) {
  const [previewing, setPreviewing] = useState<Citation | null>(null);

  if (citations.length === 0) return null;

  return (
    <div>
      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-subtle">Sources</p>
      <ul className="flex flex-col gap-1.5">
        {citations.map((citation, index) => (
          <li key={citation.chunkId}>
            <button
              type="button"
              onClick={() => setPreviewing(citation)}
              className="group flex w-full items-start gap-3 rounded-lg border border-border bg-slate-50/70 px-3 py-2.5 text-left text-xs text-ink hover:border-accent-border hover:bg-accent-subtle"
            >
              <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-white font-semibold text-accent shadow-subtle">{index + 1}</span>
              <span className="min-w-0 flex-1"><span className="mb-1 flex items-center gap-1.5 font-medium text-ink"><FileText className="h-3.5 w-3.5 text-ink-subtle" />Evidence excerpt</span><span className="line-clamp-2 leading-5 text-ink-muted">{citation.excerpt}</span></span>
              <ExternalLink className="mt-1 h-3.5 w-3.5 shrink-0 text-ink-subtle group-hover:text-accent" />
            </button>
          </li>
        ))}
      </ul>

      <EvidencePreviewModal citation={previewing} onClose={() => setPreviewing(null)} />
    </div>
  );
}
