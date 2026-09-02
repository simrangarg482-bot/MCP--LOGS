import { Fragment, type ReactNode } from "react";
import type { Citation } from "@/types/ask";
import { cn } from "@/utils/cn";

/**
 * Renders a grounded answer with its inline `[n]` citation markers turned
 * into clickable superscript chips that open the matching source.
 *
 * The Answer Agent's generation prompt requires every factual claim to
 * carry a `[n]` marker keyed to `citations[n - 1]`
 * (`app.agents.answer.generation` / `app.agents.answer.citations`), so this
 * is a presentation concern only -- no marker is invented, and a marker
 * with no corresponding citation is left as plain text rather than a dead
 * link.
 */
const MARKER_PATTERN = /\[(\d+(?:\s*,\s*\d+)*)\]/g;

export function AnswerText({
  text,
  citations,
  onCitationClick,
}: {
  text: string;
  citations: Citation[];
  onCitationClick: (citation: Citation, index: number) => void;
}) {
  const segments: ReactNode[] = [];
  let lastIndex = 0;
  let key = 0;

  for (const match of text.matchAll(MARKER_PATTERN)) {
    const matchStart = match.index ?? 0;
    if (matchStart > lastIndex) {
      segments.push(<Fragment key={key++}>{text.slice(lastIndex, matchStart)}</Fragment>);
    }

    const numbers = match[1].split(",").map((part) => Number(part.trim()));
    const resolved = numbers
      .map((n) => ({ n, citation: citations[n - 1] }))
      .filter((entry): entry is { n: number; citation: Citation } => Boolean(entry.citation));

    if (resolved.length === 0) {
      // Marker with no backing citation -- keep the literal text, don't
      // render an inert chip.
      segments.push(<Fragment key={key++}>{match[0]}</Fragment>);
    } else {
      segments.push(
        <span key={key++} className="mx-0.5 inline-flex gap-0.5 align-super">
          {resolved.map(({ n, citation }) => (
            <button
              key={n}
              type="button"
              onClick={() => onCitationClick(citation, n - 1)}
              title={`Source ${n}`}
              aria-label={`Show source ${n}`}
              className={cn(
                "inline-flex h-4 min-w-4 items-center justify-center rounded-[5px] px-1",
                "text-[10px] font-semibold leading-none text-accent",
                "bg-accent-subtle ring-1 ring-inset ring-accent-border",
                "transition hover:bg-accent hover:text-white hover:ring-accent",
                "focus-visible:ring-2 focus-visible:ring-accent",
              )}
            >
              {n}
            </button>
          ))}
        </span>,
      );
    }

    lastIndex = matchStart + match[0].length;
  }

  if (lastIndex < text.length) {
    segments.push(<Fragment key={key++}>{text.slice(lastIndex)}</Fragment>);
  }

  return (
    <p className="max-w-3xl whitespace-pre-wrap text-sm leading-6 text-ink [word-break:break-word]">
      {segments}
    </p>
  );
}
