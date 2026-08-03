"""Postmortem Agent step 1: timeline reconstruction (AGENT_WORKFLOWS.md
section 2.5 / PROJECT_PLAN.md section 6.5) -- "reads `incident_timeline`,
merging human notes and any Investigation Agent evidence attached to the
incident, into chronological narrative form."

The "merging" itself needs no code here: `core.incidents.service.get_timeline`
already returns every entry (human `"note"`s and agent-authored
`"investigation"`s alike -- see `core.incidents.service.record_investigation_
result`) in one chronologically-ordered list, since both event types share
the same `incident_timeline` table. What this module does is render that
already-merged list into the narrative text form the two downstream LLM
calls (`root_cause.extract_root_cause`, `action_items.generate_action_items`)
consume, and extract the most recent investigation's hypotheses as
`root_cause.extract_root_cause`'s candidate input.
"""

from __future__ import annotations

from typing import Any

from app.core.incidents.schemas import TimelineEntry

_EXCERPT_MAX_CHARS = 300


def build_narrative(entries: list[TimelineEntry]) -> str:
    """Render `entries` (already chronological) as one narrative text block,
    one line per entry, each tagged with its timestamp, event type, and
    authoring actor -- `[timestamp] event_type (actor): summary`.

    Returns an explicit "(no timeline entries recorded)" placeholder for an
    empty timeline rather than an empty string -- the downstream LLM prompts
    always splice this into a "Timeline:\n{narrative}" section, and an empty
    string there reads as a formatting bug, not a meaningful "nothing
    happened yet" signal.
    """
    if not entries:
        return "(no timeline entries recorded)"

    lines = [
        f"[{entry.occurred_at.isoformat()}] {entry.event_type} ({entry.actor}): "
        f"{_summarize_event_data(entry.event_type, entry.event_data)}"
        for entry in entries
    ]
    return "\n".join(lines)


def latest_investigation_hypotheses(entries: list[TimelineEntry]) -> list[dict[str, Any]]:
    """Return the `hypotheses` list from the most recent `"investigation"`
    timeline entry, or `[]` if none exists -- `root_cause.extract_root_cause`'s
    candidate root-cause input (AGENT_WORKFLOWS.md section 2.5: "if an
    Investigation Agent hypothesis exists for this incident ... it's the
    starting point for `root_cause`").

    The *most recent* investigation, not the first or all of them: a later
    triage run superseding an earlier one (e.g. the first found nothing, a
    second run after more evidence accumulated found something) should be
    the one whose hypotheses are actually considered current.
    """
    for entry in reversed(entries):
        if entry.event_type == "investigation":
            hypotheses = entry.event_data.get("hypotheses", [])
            return hypotheses if isinstance(hypotheses, list) else []
    return []


def _summarize_event_data(event_type: str, event_data: dict[str, Any]) -> str:
    """Render one entry's `event_data` as a short, human-readable summary
    line. `"note"` and `"investigation"` (the two event types this codebase
    actually writes today -- `add_timeline_note`, `record_investigation_
    result`) get a tailored rendering; any other `event_type` falls back to
    the raw dict so a future write path is still visible in the narrative
    rather than silently dropped.
    """
    if event_type == "note":
        note = event_data.get("note", "")
        return str(note)[:_EXCERPT_MAX_CHARS]

    if event_type == "investigation":
        hypotheses = event_data.get("hypotheses", [])
        evidence = event_data.get("evidence", [])
        if hypotheses:
            top = hypotheses[0]
            return (
                f"{len(evidence)} evidence item(s) gathered; top hypothesis "
                f"(confidence {top.get('confidence', 0):.2f}): "
                f"{str(top.get('description', ''))[:_EXCERPT_MAX_CHARS]}"
            )
        return f"{len(evidence)} evidence item(s) gathered; no supported hypothesis produced"

    return str(event_data)[:_EXCERPT_MAX_CHARS]
