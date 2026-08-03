"""Document Processing stage 1: strip noise from a connector's raw content
before anything downstream (metadata extraction, chunking) sees it.

Owned by: ingestion/processors/. Per PROJECT_PLAN.md section 4.6, this is
part of "Document Processing" (strip noise, extract metadata, attach
organization_id/project_id) -- kept in its own module rather than folded
into metadata.py, since "what does clean content look like" and "what
metadata do we keep" are independently testable, differently-scoped
concerns despite both feeding the same pipeline stage.

Deliberately generic, not per-source: PROJECT_PLAN.md section 4.1 keeps
source-specific interpretation inside each connector's `normalize()` (e.g.
`SlackConnector` already strips its own `_channel_id` bookkeeping before
this stage ever runs) -- what's left here is noise that can show up in
*any* source's raw text (HTML entities/tags leaking through, excessive
blank lines, control characters), not source-specific formatting.
"""

from __future__ import annotations

import html
import re

# Collapses 3+ consecutive newlines down to a max of 2 (one blank line) --
# preserves paragraph breaks (a real structural signal the document chunker
# relies on) without letting large blocks of near-empty content pad out
# chunk boundaries for no reason.
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")
_HTML_TAG = re.compile(r"<[^>]+>")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean_content(raw_content: str) -> str:
    """Strip HTML markup, decode HTML entities, drop control characters, and
    collapse excessive blank lines.

    Known limitation, flagged rather than silently ignored: Slack-specific
    markup (`<@U123>` mentions, `<#C123|general>` channel links, `<url|text>`
    link wrapping) is NOT unwrapped here. Doing that well needs a
    user/channel-ID -> display-name lookup this pipeline stage has no access
    to (that's Slack API data, not something derivable from the message text
    alone) -- left as a documented follow-up rather than half-built with IDs
    that don't resolve to anything readable.
    """
    text = html.unescape(raw_content)
    text = _HTML_TAG.sub(" ", text)
    text = _CONTROL_CHARS.sub("", text)
    text = _EXCESS_BLANK_LINES.sub("\n\n", text)
    # Trim trailing whitespace per line (a common HTML/export artifact)
    # without collapsing intentional leading indentation, which would break
    # code content.
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()
