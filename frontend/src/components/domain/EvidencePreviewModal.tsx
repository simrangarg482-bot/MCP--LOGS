import { Modal } from "@/components/ui/Modal";
import type { Citation } from "@/types/ask";

/**
 * Shows the real, already-fetched `Citation` fields
 * (`app.shared.schemas.agent_contracts.Citation`: `documentId`, `chunkId`,
 * `sourceUrl`, `excerpt`) -- no new backend endpoint was added, and no
 * fictional `CitationSource` model was resurrected. There is genuinely no
 * general "fetch a document/chunk by id" REST endpoint tenant-scoped
 * evidence like this could call for more detail (`GET /knowledge/{id}`
 * only covers published/proposed *knowledge* documents, not arbitrary
 * retrieval-collection chunks a citation may point at) -- the excerpt
 * already returned with the answer IS the full available detail, so this
 * is a synchronous, client-side-only preview: no fetch, no loading state,
 * no error state to show, because nothing is fetched.
 */
export function EvidencePreviewModal({
  citation,
  onClose,
}: {
  citation: Citation | null;
  onClose: () => void;
}) {
  return (
    <Modal open={citation !== null} onClose={onClose} title="Evidence">
      {citation && (
        <div className="flex flex-col gap-3">
          <div>
            <p className="mb-1 text-xs font-medium uppercase tracking-wide text-ink-subtle">Excerpt</p>
            <p className="whitespace-pre-wrap rounded-md border border-border bg-slate-50 px-3 py-2.5 text-sm text-ink">
              {citation.excerpt}
            </p>
          </div>

          <dl className="grid grid-cols-2 gap-2 text-xs">
            <div>
              <dt className="text-ink-subtle">Document</dt>
              <dd className="font-mono text-ink">{citation.documentId.slice(0, 8)}</dd>
            </div>
            <div>
              <dt className="text-ink-subtle">Chunk</dt>
              <dd className="font-mono text-ink">{citation.chunkId.slice(0, 8)}</dd>
            </div>
          </dl>

          {citation.sourceUrl ? (
            <a
              href={citation.sourceUrl}
              target="_blank"
              rel="noreferrer"
              className="text-xs font-medium text-accent hover:underline"
            >
              Open source →
            </a>
          ) : (
            <p className="text-xs text-ink-subtle">No source URL available for this evidence.</p>
          )}
        </div>
      )}
    </Modal>
  );
}
