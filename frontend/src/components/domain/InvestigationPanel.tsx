import { Sparkles } from "lucide-react";
import type { InvestigationResult } from "@/types/ask";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { formatPercent } from "@/utils/format";
import { formatDateTime, formatRelativeTime } from "@/utils/date";

/**
 * Renders a real `InvestigationResult` (`app.shared.schemas.agent_contracts.
 * InvestigationResult`, exactly what `POST /incidents/{id}/investigate`
 * returns on `AskResponse.investigation`) -- replaces `AIAnalysisPanel`,
 * which rendered a fictional `AiInvestigation` shape (`summary`,
 * `rootCauseHypotheses`, `relevantKnowledge`, `similarIncidents`,
 * `recommendedActions`, a `confidence`/`model`/`generatedAt` the backend
 * never returns) matching nothing this endpoint actually sends back.
 */
export function InvestigationPanel({ investigation }: { investigation: InvestigationResult }) {
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <span className="flex h-6 w-6 items-center justify-center rounded-md bg-accent-subtle text-accent">
            <Sparkles className="h-3.5 w-3.5" />
          </span>
          <CardTitle>Investigation result</CardTitle>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <section>
          <p className="mb-1.5 text-xs font-medium uppercase tracking-wide text-ink-subtle">Verified evidence</p>
          {investigation.evidence.length === 0 ? (
            <p className="text-sm text-ink-muted">No automated evidence found.</p>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {investigation.evidence.map((item, index) => (
                <li key={index} className="rounded-md border border-border bg-white px-2.5 py-2 text-sm text-ink">
                  <div className="mb-0.5 flex items-center gap-1.5">
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-ink-muted">
                      {item.source}
                    </span>
                    <span className="text-xs text-ink-muted">{item.reference}</span>
                    {item.sourceTimestamp && (
                      <span
                        className="ml-auto shrink-0 text-xs text-ink-subtle"
                        title={formatDateTime(item.sourceTimestamp)}
                      >
                        {formatRelativeTime(item.sourceTimestamp)}
                      </span>
                    )}
                  </div>
                  <p>{item.summary}</p>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section>
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-subtle">
            Hypotheses (AI-generated, unverified)
          </p>
          {investigation.hypotheses.length === 0 ? (
            <p className="text-sm text-ink-muted">No hypotheses generated -- review the evidence manually.</p>
          ) : (
            <ol className="flex flex-col gap-2">
              {investigation.hypotheses.map((hypothesis, index) => (
                <li key={index} className="rounded-md border border-border bg-slate-50 px-3 py-2.5">
                  <div className="flex items-start justify-between gap-3">
                    <p className="text-sm font-medium text-ink">{hypothesis.description}</p>
                    <span className="shrink-0 text-xs font-medium text-ink-muted">
                      {formatPercent(hypothesis.confidence)}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-ink-subtle">
                    Supported by: {hypothesis.supportingEvidenceIds.join(", ")}
                  </p>
                </li>
              ))}
            </ol>
          )}
        </section>

        {investigation.suggestedOwnerTeam && (
          <section>
            <p className="text-xs text-ink-muted">
              Suggested owner:{" "}
              <span className="font-medium text-ink">{investigation.suggestedOwnerTeam}</span>
              <span className="ml-1 text-ink-subtle">(recommendation only, not confirmed ownership)</span>
            </p>
          </section>
        )}

        {investigation.suggestedNextSteps.length > 0 && (
          <section>
            <p className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-subtle">Suggested next steps</p>
            <ul className="flex flex-col gap-1.5">
              {investigation.suggestedNextSteps.map((step, index) => (
                <li key={index} className="flex items-start gap-2 text-sm text-ink">
                  <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />
                  {step}
                </li>
              ))}
            </ul>
          </section>
        )}
      </CardContent>
    </Card>
  );
}
