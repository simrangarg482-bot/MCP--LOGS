import { useState } from "react";
import { Link2 } from "lucide-react";
import type { Citation } from "@/types/ask";
import { EvidencePreviewModal } from "./EvidencePreviewModal";

export function AskCitationList({ citations }: { citations: Citation[] }) {
  const [previewing, setPreviewing] = useState<Citation | null>(null);

  if (citations.length === 0) return null;

  return (
    <div>
      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-subtle">Sources</p>
      <ul className="flex flex-col gap-1.5">
        {citations.map((citation) => (
          <li key={citation.chunkId}>
            <button
              type="button"
              onClick={() => setPreviewing(citation)}
              className="flex w-full items-start gap-2 rounded-md border border-border bg-white px-2.5 py-1.5 text-left text-xs text-ink hover:border-accent-border hover:bg-accent-subtle"
            >
              <Link2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-muted" />
              <span className="line-clamp-2">{citation.excerpt}</span>
            </button>
          </li>
        ))}
      </ul>

      <EvidencePreviewModal citation={previewing} onClose={() => setPreviewing(null)} />
    </div>
  );
}
