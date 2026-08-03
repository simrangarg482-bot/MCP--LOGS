"""Context assembly -- stage 4 of the Retrieval Agent (AGENT_WORKFLOWS.md
section 2.1 step 4 / PROJECT_PLAN.md section 6.1): trims the reranked
candidate set to a token budget, preserving each chunk's
`source_offset_start`/`source_offset_end` untouched so every surviving chunk
can still become a `Citation` with a real, offset-anchored excerpt.

Token counting is a rough `len(content) // 4` heuristic (the commonly-cited
approximation for English text), not an exact tokenizer count -- no
tokenizer library (e.g. `tiktoken`) is pinned in this project, and an
approximate budget is enough to bound context size without adding a new
dependency for a stage that only needs to stay in the right ballpark.
"""

from __future__ import annotations

from app.retrieval.schemas import ScoredChunk

_CHARS_PER_TOKEN = 4  # rough heuristic, see module docstring
_DEFAULT_TOKEN_BUDGET = 4000


def assemble_context(
    chunks: list[ScoredChunk], *, token_budget: int = _DEFAULT_TOKEN_BUDGET
) -> list[ScoredChunk]:
    """Greedily keep `chunks` (already rank-ordered by the caller) until
    adding the next one would exceed `token_budget` estimated tokens.

    Always keeps at least the first chunk, even if it alone exceeds the
    budget -- an empty context is a worse outcome than one slightly-over-
    budget chunk.
    """
    if not chunks:
        return []

    kept: list[ScoredChunk] = []
    used_tokens = 0
    for chunk in chunks:
        estimated_tokens = max(1, len(chunk.content) // _CHARS_PER_TOKEN)
        if kept and used_tokens + estimated_tokens > token_budget:
            break
        kept.append(chunk)
        used_tokens += estimated_tokens
    return kept
