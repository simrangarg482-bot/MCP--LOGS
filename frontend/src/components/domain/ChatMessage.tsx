import { useState } from "react";
import { Check, Copy, RefreshCw, Sparkles, Loader2, AlertTriangle, User as UserIcon } from "lucide-react";
import type { ChatTurn } from "@/types/ask";
import { AskCitationList } from "./AskCitationList";
import { formatPercent } from "@/utils/format";
import { cn } from "@/utils/cn";
import { Button } from "@/components/ui/Button";

function ConfidenceBadge({ value }: { value: number }) {
  const tone = value >= 0.75 ? "text-success" : value >= 0.5 ? "text-warning" : "text-critical";
  return <span className={cn("text-xs font-medium", tone)}>{formatPercent(value)} confidence</span>;
}

export function ChatMessage({ turn, onRetry }: { turn: ChatTurn; onRetry?: () => void }) {
  const [copied, setCopied] = useState(false);

  async function copyAnswer() {
    const content = turn.response?.answer;
    if (!content) return;
    await navigator.clipboard.writeText(content);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-start justify-end gap-2.5">
        <div className="max-w-2xl rounded-2xl rounded-tr-sm bg-accent px-4 py-3 text-sm leading-5 text-white shadow-sm">
          {turn.question}
        </div>
        <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-slate-100 text-ink-muted">
          <UserIcon className="h-3.5 w-3.5" />
        </div>
      </div>

      <div className="flex items-start gap-2.5">
        <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-accent-subtle text-accent">
          <Sparkles className="h-3.5 w-3.5" />
        </div>
        <div className="max-w-3xl flex-1 rounded-2xl rounded-tl-sm border border-border bg-surface px-4 py-4 shadow-subtle sm:px-5">
          {turn.isPending && (
            <div className="flex items-center gap-2 text-sm text-ink-muted">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              EKIP is thinking…
            </div>
          )}

          {turn.error && !turn.isPending && (
            <div className="flex flex-wrap items-center gap-3 text-sm text-critical">
              <span className="flex items-start gap-2"><AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />{turn.error}</span>
              {onRetry && <Button size="sm" variant="secondary" onClick={onRetry}><RefreshCw className="h-3.5 w-3.5" />Retry</Button>}
            </div>
          )}

          {turn.response && !turn.isPending && (
            <div className="flex flex-col gap-3">
              <div className="flex items-center justify-between gap-3">
                <span className="rounded-full border border-border bg-slate-50 px-2.5 py-1 text-[11px] font-medium text-ink-muted">{turn.response.routeTaken === "answer" ? "Direct answer" : "Investigation"}</span>
                <ConfidenceBadge value={turn.response.confidence} />
              </div>

              {turn.response.routeTaken === "answer" && turn.response.answer && (
                <div>
                  <p className="whitespace-pre-wrap text-sm leading-6 text-ink">{turn.response.answer}</p>
                  <Button size="sm" variant="ghost" onClick={() => void copyAnswer()} className="mt-2 -ml-2 text-ink-subtle">
                    {copied ? <Check className="h-3.5 w-3.5 text-success" /> : <Copy className="h-3.5 w-3.5" />}{copied ? "Copied" : "Copy answer"}
                  </Button>
                </div>
              )}

              {turn.response.routeTaken === "investigation" && turn.response.investigation && (
                <div className="flex flex-col gap-3">
                  <div className="rounded-md border border-warning-border bg-warning-subtle px-3 py-2 text-xs text-warning">
                    Confidence was too low for a direct answer -- EKIP escalated to an investigation.
                  </div>

                  {turn.response.investigation.evidence.length > 0 && (
                    <div>
                      <p className="mb-1.5 text-xs font-medium uppercase tracking-wide text-ink-subtle">
                        Verified evidence
                      </p>
                      <ul className="flex flex-col gap-1.5">
                        {turn.response.investigation.evidence.map((item, index) => (
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

                  {turn.response.investigation.hypotheses.length > 0 && (
                    <div>
                      <p className="mb-1.5 text-xs font-medium uppercase tracking-wide text-ink-subtle">
                        Hypotheses (AI-generated, unverified)
                      </p>
                      <ul className="flex flex-col gap-1.5">
                        {turn.response.investigation.hypotheses.map((hypothesis, index) => (
                          <li
                            key={index}
                            className="rounded-md border border-border bg-slate-50 px-2.5 py-2 text-sm text-ink"
                          >
                            <div className="flex items-start justify-between gap-2">
                              <span>{hypothesis.description}</span>
                              <span className="shrink-0 text-xs font-medium text-ink-muted">
                                {formatPercent(hypothesis.confidence)}
                              </span>
                            </div>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {turn.response.investigation.suggestedNextSteps.length > 0 && (
                    <div>
                      <p className="mb-1.5 text-xs font-medium uppercase tracking-wide text-ink-subtle">
                        Suggested next steps
                      </p>
                      <ul className="flex flex-col gap-1">
                        {turn.response.investigation.suggestedNextSteps.map((step, index) => (
                          <li key={index} className="flex items-start gap-2 text-sm text-ink">
                            <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />
                            {step}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {turn.response.investigation.suggestedOwnerTeam && (
                    <p className="text-xs text-ink-muted">
                      Suggested owner:{" "}
                      <span className="font-medium text-ink">
                        {turn.response.investigation.suggestedOwnerTeam}
                      </span>
                    </p>
                  )}
                </div>
              )}

              <AskCitationList citations={turn.response.citations} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
