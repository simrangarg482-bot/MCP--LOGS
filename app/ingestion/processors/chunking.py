"""Document Processing stage 3: split cleaned content into retrieval-sized
`Chunk`s, preserving source-anchored offsets for citation.

Owned by: ingestion/processors/. Per PROJECT_PLAN.md section 4.6, chunking
strategy is chosen by *content type*, not by source -- code arriving via
GitHub or (eventually) Azure DevOps should chunk the same way. This module
classifies a `RawDocument` into a `ContentType` first, independent of which
connector produced it, then dispatches to the matching strategy.

Chunks are NOT persisted by ingestion itself -- `<collection>_chunks` tables
are retrieval-owned (DATABASE_DESIGN.md's ownership convention). `Chunk` is
a pure in-memory/transport shape this module produces; `ingestion/service.py`
hands the result to `retrieval.service.upsert(chunks)` (Milestone 5).

`ContentType` itself is defined in `app.ingestion.schemas`, not here --
`ProcessedDocument` (that module) carries a `content_type` field alongside
`chunks`, which would create an import cycle if this module owned the type
instead. This module still owns `classify_content_type`, the only function
that produces a `ContentType` value.

`_MAX_CHUNK_CHARS` is a first-pass constant, not a tuned value: real chunk
sizing depends on the embedding model's context window, which
DATABASE_DESIGN.md's own "Open items" section lists as not yet pinned.
Every strategy below falls back to size-based splitting once a natural
boundary (function, message, heading) still produces an oversized piece, so
no chunk this module produces can exceed the limit regardless of source
structure.
"""

from __future__ import annotations

import re

from app.ingestion.schemas import Chunk, ContentType, RawDocument

_MAX_CHUNK_CHARS = 2000  # ~roughly 400-500 tokens; see module docstring.

_CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".rb", ".rs", ".c",
    ".h", ".cpp", ".hpp", ".cs", ".php", ".swift", ".kt", ".scala", ".sql",
}
# One shared source set for both Slack (today) and any future chat-shaped
# source (Teams, per PROJECT_PLAN.md section 4) -- classification is by
# *source* for chat, since "is this a chat message" isn't derivable from
# content alone the way "is this a code file" is (a file extension exists;
# a chat message has none).
_CHAT_SOURCES = {"slack", "teams"}

# Same source-based classification as chat, and for the same reason: an
# incident's title+description+resolution text has no file extension and
# no shape that would otherwise distinguish it from a plain "documentation"
# doc -- only `app.ingestion.connectors.incidents.IncidentsConnector.
# source_name` identifies it. Kept as its own set (not folded into
# `_CHAT_SOURCES`) since "incident" and "chat" are different `ContentType`s.
_INCIDENT_SOURCES = {"incidents"}

# Matches a plausible top-level function/class definition line across
# several common languages -- a deliberately simple heuristic, not a real
# per-language parser (tree-sitter or similar would be the correct long-term
# answer, and a meaningfully larger dependency than this first pass
# warrants). Anchored to column 0 (or up to one level of indentation) so a
# nested helper function inside a class body doesn't fragment that class's
# own chunk.
_CODE_BOUNDARY = re.compile(
    r"^(?:\s{0,4})(?:def |class |function |func |public |private |protected |"
    r"impl |struct |interface )",
    re.MULTILINE,
)

# Matches a Markdown heading line ("#", "##", ...).
_HEADING_BOUNDARY = re.compile(r"^#{1,6}\s+.+$", re.MULTILINE)


def classify_content_type(raw_document: RawDocument) -> ContentType:
    """Decide which chunking strategy applies -- by content shape, not by
    which connector produced the document (see module docstring). Two
    exceptions, both source-based rather than shape-based, for the same
    reason: chat messages and incident records have no content shape that
    distinguishes them from prose documentation on their own.
    """
    if raw_document.source in _CHAT_SOURCES:
        return "chat"
    if raw_document.source in _INCIDENT_SOURCES:
        return "incident"
    path = raw_document.metadata.get("path") or raw_document.title or raw_document.external_id
    if any(path.endswith(ext) for ext in _CODE_EXTENSIONS):
        return "code"
    return "document"


def chunk_document(
    content: str, content_type: ContentType, *, max_chars: int = _MAX_CHUNK_CHARS
) -> list[Chunk]:
    """Split `content` into `Chunk`s using the strategy for `content_type`,
    each carrying its exact character offsets into `content` for citation.

    `content` is expected to already be the *cleaned* output of
    `processors.cleaning.clean_content` -- offsets are only meaningful
    relative to whatever string is actually passed in here.
    """
    if content_type == "code":
        boundaries = _find_boundaries(content, _CODE_BOUNDARY)
    elif content_type == "chat":
        # A connector already normalizes one chat message into one
        # RawDocument (PROJECT_PLAN.md section 4.2's normalize() step), so
        # "chunk by message boundary" has effectively already happened
        # upstream by the time content reaches here -- there is only one
        # "message" in this content, spanning the whole string.
        boundaries = [0]
    elif content_type == "incident":
        # Same reasoning as chat: `IncidentsConnector.normalize()` already
        # produces one coherent unit (title + description + resolution) per
        # incident -- splitting it by heading would fragment a single
        # incident's symptoms from its own resolution, which is exactly the
        # semantic unit "similar past incidents" search needs kept whole.
        # `_split_oversized` below still splits it further if it's long
        # enough to need that, same as every other strategy.
        boundaries = [0]
    else:
        boundaries = _find_boundaries(content, _HEADING_BOUNDARY)

    sections = _slice_by_boundaries(content, boundaries)

    chunks: list[Chunk] = []
    for section_start, section_text in sections:
        for piece_start, piece_text in _split_oversized(section_text, max_chars):
            absolute_start = section_start + piece_start
            chunks.append(
                Chunk(
                    chunk_index=len(chunks),
                    content=piece_text,
                    source_offset_start=absolute_start,
                    source_offset_end=absolute_start + len(piece_text),
                )
            )
    return chunks


def _find_boundaries(content: str, pattern: re.Pattern[str]) -> list[int]:
    """Character offsets where each structural boundary (a heading, a
    function/class definition line) begins. Always includes 0, so content
    before the first real match (an unlabeled preamble) still becomes its
    own leading section instead of being dropped.
    """
    offsets = [match.start() for match in pattern.finditer(content)]
    if not offsets or offsets[0] != 0:
        offsets = [0, *offsets]
    return offsets


def _slice_by_boundaries(content: str, boundaries: list[int]) -> list[tuple[int, str]]:
    """Slice `content` into `(start_offset, text)` sections between
    consecutive boundaries, dropping any section that's empty/whitespace-only
    (e.g. two heading lines directly adjacent, with nothing between them).
    """
    sections = []
    for i, start in enumerate(boundaries):
        end = boundaries[i + 1] if i + 1 < len(boundaries) else len(content)
        section_text = content[start:end]
        if section_text.strip():
            sections.append((start, section_text))
    return sections


def _split_oversized(text: str, max_chars: int) -> list[tuple[int, str]]:
    """Fallback for any single section (a function, a heading's section)
    that still exceeds `max_chars`: group paragraphs (blank-line-separated)
    up to the limit, falling back further to a hard character-count slice
    for any single paragraph that alone exceeds it (e.g. a minified file, a
    very long function with no internal blank lines).

    Returns `(offset_within_text, piece)` pairs so the caller can translate
    back to absolute offsets into the original document. Offsets are
    located via `str.find` rather than reconstructed from paragraph
    lengths, so this stays correct regardless of the exact whitespace
    separating paragraphs.
    """
    if len(text) <= max_chars:
        return [(0, text)]

    paragraphs: list[tuple[int, str]] = []
    search_from = 0
    for paragraph in text.split("\n\n"):
        offset = text.find(paragraph, search_from)
        paragraphs.append((offset, paragraph))
        search_from = offset + len(paragraph)

    grouped: list[tuple[int, str]] = []
    group_start, group_text = paragraphs[0]
    for offset, paragraph in paragraphs[1:]:
        candidate = f"{group_text}\n\n{paragraph}"
        if len(candidate) > max_chars:
            grouped.append((group_start, group_text))
            group_start, group_text = offset, paragraph
        else:
            group_text = candidate
    grouped.append((group_start, group_text))

    final_pieces: list[tuple[int, str]] = []
    for offset, piece in grouped:
        if len(piece) <= max_chars:
            final_pieces.append((offset, piece))
        else:
            for slice_start in range(0, len(piece), max_chars):
                final_pieces.append(
                    (offset + slice_start, piece[slice_start : slice_start + max_chars])
                )
    return final_pieces
