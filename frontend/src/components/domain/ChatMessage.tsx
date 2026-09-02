import { useState } from "react";
import {
  AlertTriangle,
  Check,
  Copy,
  Loader2,
  RefreshCw,
  Search,
  Sparkles,
  User as UserIcon,
} from "lucide-react";
import type { ChatTurn } from "@/types/ask";
import type { Citation } from "@/types/ask";
import { AnswerText } from "./AnswerText";
import { AskCitationList } from "./AskCitationList";
import { EvidencePreviewModal } from "./EvidencePreviewModal";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { formatPercent } from "@/utils/format";
import { cn } from "@/utils/cn";

function ConfidenceMeter({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const tone =
    value >= 0.75
      ? { bar: "bg-success", text: "text-success" }
      : value >= 0.5
        ? { bar: "bg-warning", text: "text-warning" }
        : { bar: "bg-critical", text: "text-critical" };
  return (
    <span className="inline-flex items-center gap-2" title={`Model confidence: ${pct}%`}>
      <span className="h-1.5 w-14 overflow-hidden rounded-full bg-slate-200" aria-hidden>
        <span className={cn("block h-full rounded-full", tone.bar)} style={{ width: `${pct}%` }} />
      </span>
      <span className={cn("text-xs font-medium tabular-nums", tone.text)}>
        {formatPercent(value)} confidence
      </span>
    </span>
  );
}

export function ChatMessage({ turn, onRetry }: { turn: ChatTurn; onRetry?: () => void }) {
  const [copied, setCopied] = useState(false);
  const [previewing, setPreviewing] = useState<Citation | null>(null);

  const response = turn.response;
  const isDecline =
    !!response &&
    response.routeTaken === "answer" &&
    (response.answerMode === "no_answer" || !response.answer);

  async function copyAnswer() {
    if (!response?.answer) return;
    await navigator.clipboard.writeText(response.answer);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  }

  return (
    <div className="flex flex-col gap-3">
      {/* user turn */}
      <div className="flex items-start justify-end gap-2.5">
        <div className="max-w-xl whitespace-pre-wrap rounded-2xl rounded-tr-sm bg-accent px-4 py-3 text-sm leading-5 text-white shadow-sm [word-break:break-word]">
          {turn.question}
        </div>
        <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-slate-100 text-ink-muted">
          <UserIcon className="h-3.5 w-3.5" />
        </div>
      </div>

      {/* assistant turn */}
      <div className="flex items-start gap-2.5">
        <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-accent-subtle text-accent ring-1 ring-inset ring-accent-border">
          <Sparkles className="h-3.5 w-3.5" />
        </div>
        <div className="min-w-0 flex-1 rounded-2xl rounded-tl-sm border border-border bg-surface shadow-subtle">
          {turn.isPending && (
            <div className="flex items-center gap-2.5 px-4 py-4 text-sm text-ink-muted sm:px-5">
              <Loader2 className="h-3.5 w-3.5 animate-spin text-accent" />
              <span>Searching connected sources and checking evidence…</span>
            </div>
          )}

          {turn.error && !turn.isPending && (
            <div className="flex flex-wrap items-center gap-3 px-4 py-4 text-sm text-critical sm:px-5">
              <span className="flex items-start gap-2">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                {turn.error}
              </span>
              {onRetry && (
                <Button size="sm" variant="secondary" onClick={onRetry}>
                  <RefreshCw className="h-3.5 w-3.5" />
                  Retry
                </Button>
              )}
            </div>
          )}

          {response && !turn.isPending && (
            <div className="flex flex-col">
              {/* meta row */}
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5 sm:px-5">
                <Badge tone={response.routeTaken === "answer" ? "accent" : "warning"}>
                  {response.routeTaken === "answer" ? (
                    <>
                      <Sparkles className="h-3 w-3" />
                      {isDecline ? "No confident answer" : "Answered"}
                    </>
                  ) : (
                    <>
                      <Search className="h-3 w-3" />
                      Investigation
                    </>
                  )}
                </Badge>
                <ConfidenceMeter value={response.confidence} />
              </div>

              <div className="flex flex-col gap-3 px-4 py-4 sm:px-5">
                {/* direct answer */}
                {response.routeTaken === "answer" && !isDecline && response.answer && (
                  <div>
                    <AnswerText
                      text={response.answer}
                      citations={response.citations}
                      onCitationClick={(citation) => setPreviewing(citation)}
                    />
                    <div className="mt-3 flex items-center gap-3 border-t border-border pt-2.5">
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => void copyAnswer()}
                        className="-ml-2 text-ink-subtle"
                      >
                        {copied ? (
                          <Check className="h-3.5 w-3.5 text-success" />
                        ) : (
                          <Copy className="h-3.5 w-3.5" />
                        )}
                        {copied ? "Copied" : "Copy answer"}
                      </Button>
                      {response.citations.length > 0 && (
                        <span className="text-xs text-ink-subtle">
                          Grounded in {response.citations.length}{" "}
                          {response.citations.length === 1 ? "source" : "sources"}
                        </span>
                      )}
                    </div>
                  </div>
                )}

                {/* honest decline on the answer route */}
                {isDecline && (
                  <div className="rounded-lg border border-border bg-slate-50 px-3.5 py-3 text-sm text-ink-muted">
                    <p className="font-medium text-ink">EKIP didn&rsquo;t find enough grounded evidence to answer this confidently.</p>
                    <p className="mt-1 leading-5">
                      Try rephrasing with more specific terms, or check that the relevant source is
                      connected and ingested.
                    </p>
                  </div>
                )}

                {/* investigation route */}
                {response.routeTaken === "investigation" && response.investigation && (
                  <div className="flex flex-col gap-3">
                    <div className="rounded-md border border-warning-border bg-warning-subtle px-3 py-2 text-xs text-warning">
                      Confidence was too low for a direct answer &mdash; EKIP escalated to an
                      investigation.
                    </div>

                    {response.investigation.evidence.length > 0 && (
                      <div>
                        <p className="mb-1.5 text-xs font-medium uppercase tracking-wide text-ink-subtle">
                          Verified evidence
                        </p>
                        <ul className="flex flex-col gap-1.5">
                          {response.investigation.evidence.map((item, index) => (
                            <li
                              key={index}
                              className="rounded-md border border-border bg-white px-2.5 py-2 text-sm text-ink"
                            >
                              <div className="mb-0.5 flex items-center gap-1.5">
                                <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-ink-muted">
                                  {item.source}
                                </span>
                                <span className="text-xs text-ink-muted">{item.reference}</span>
                              </div>
                              <p className="text-sm text-ink">{item.summary}</p>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {response.investigation.hypotheses.length > 0 && (
                      <div>
                        <p className="mb-1.5 text-xs font-medium uppercase tracking-wide text-ink-subtle">
                          Hypotheses <span className="font-normal normal-case">(AI-generated, unverified)</span>
                        </p>
                        <ul className="flex flex-col gap-1.5">
                          {response.investigation.hypotheses.map((hypothesis, index) => (
                            <li
                              key={index}
                              className="rounded-md border border-border bg-slate-50 px-2.5 py-2 text-sm text-ink"
                            >
                              <div className="flex items-start justify-between gap-2">
                                <span>{hypothesis.description}</span>
                                <span className="shrink-0 text-xs font-medium tabular-nums text-ink-muted">
                                  {formatPercent(hypothesis.confidence)}
                                </span>
                              </div>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {response.investigation.suggestedNextSteps.length > 0 && (
                      <div>
                        <p className="mb-1.5 text-xs font-medium uppercase tracking-wide text-ink-subtle">
                          Suggested next steps
                        </p>
                        <ul className="flex flex-col gap-1">
                          {response.investigation.suggestedNextSteps.map((step, index) => (
                            <li key={index} className="flex items-start gap-2 text-sm text-ink">
                              <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />
                              {step}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {response.investigation.suggestedOwnerTeam && (
                      <p className="text-xs text-ink-muted">
                        Suggested owner:{" "}
                        <span className="font-medium text-ink">
                          {response.investigation.suggestedOwnerTeam}
                        </span>
                      </p>
                    )}
                  </div>
                )}

                <AskCitationList
                  citations={response.citations}
                  onPreview={(citation) => setPreviewing(citation)}
                  activeChunkId={previewing?.chunkId ?? null}
                />
              </div>
            </div>
          )}
        </div>
      </div>

      <EvidencePreviewModal citation={previewing} onClose={() => setPreviewing(null)} />
    </div>
  );
}
