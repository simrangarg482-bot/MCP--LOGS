"""Inline citation marker syntax (`[1]`, `[2]`, ...) shared by
`agents.answer.generation` (which instructs the model to produce them),
`agents.answer.citations` (which extracts them into `Citation`s), and
`agents.answer.grounding` (which strips them before embedding comparison --
a bracketed number is not semantic content to compare against retrieved
chunks).

Owned by: agents/answer/. One shared module rather than each of those three
files defining its own copy of the regex, so the marker format only needs to
change in one place.
"""

from __future__ import annotations

import re

MARKER_PATTERN = re.compile(r"\[(\d+)\]")


def extract_citation_markers(text: str) -> list[int]:
    """Return the distinct 1-indexed marker numbers referenced in `text`, in
    first-seen order.
    """
    seen: list[int] = []
    for match in MARKER_PATTERN.finditer(text):
        number = int(match.group(1))
        if number not in seen:
            seen.append(number)
    return seen


def strip_markers(text: str) -> str:
    """Remove every `[n]` marker from `text`, collapsing any resulting
    double-spaces left behind.
    """
    return re.sub(r"\s+", " ", MARKER_PATTERN.sub("", text)).strip()
