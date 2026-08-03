"""Grounding verification -- PROJECT_PLAN.md section 5.7 / AGENT_WORKFLOWS.md
section 2.3's post-generation check: every factual sentence in a generated
answer must be traceable back to a specific retrieved chunk before it can
reach the caller.

Owned by: agents/answer/. Embedding-similarity is the default check (cheap,
no extra LLM call); an LLM-based check only runs for sentences whose
similarity lands in an ambiguous middle band -- "avoiding a second full LLM
call for the common, clearly-grounded case" (PROJECT_PLAN.md section 5.7).

Sentence splitting is a plain regex heuristic, not a real NLP sentence
tokenizer (no new dependency) -- good enough for the short, single-paragraph
answers this Answer Agent produces; not intended to handle abbreviations,
decimals, or other edge cases a real tokenizer would.

Re-embeds every retrieved chunk's content on every call, rather than reusing
an already-computed vector: `ScoredChunk` (`retrieval.schemas`) carries a
relevance `score`, not the underlying embedding itself -- `VectorStore.search`
never returns raw vectors, only scored results (`retrieval.interfaces.base`).
A real but bounded inefficiency (chunk counts here are already
token-budget-capped by `agents.retrieval.context_assembly`, so this is a
handful of re-embeddings per answer, not an unbounded cost) flagged here
rather than reopening `ScoredChunk`'s already-reviewed shape for this one
caller.
"""

from __future__ import annotations

import re

from langchain_core.language_models import BaseChatModel

from app.agents.answer.markers import strip_markers
from app.retrieval import embedding
from app.retrieval.schemas import ScoredChunk
from app.shared.config.logging import get_logger

logger = get_logger(__name__)

_SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?])\s+")

# Embedding-similarity thresholds (cosine similarity -- both sides
# L2-normalized via `retrieval.embedding`, so dot product == cosine
# similarity, same reasoning `retrieval.pgvector.store` already uses).
# Placeholder values (AGENT_WORKFLOWS.md "Open items": "Grounding-check
# similarity threshold ... to be tuned once real generation data exists"),
# not empirically calibrated.
_GROUNDED_THRESHOLD = 0.55
_UNGROUNDED_THRESHOLD = 0.35


def split_sentences(text: str) -> list[str]:
    """Split `text` into sentences on `.`/`!`/`?` followed by whitespace."""
    return [sentence for sentence in _SENTENCE_SPLIT_PATTERN.split(text.strip()) if sentence]


async def verify_grounding(
    llm: BaseChatModel, sentences: list[str], chunks: list[ScoredChunk]
) -> list[str]:
    """Return the subset of `sentences` that are grounded in `chunks`,
    preserving order and each surviving sentence's original `[n]` markers.

    A sentence failing grounding is dropped entirely, per PROJECT_PLAN.md
    section 5.7: "A sentence that can't be traced is either removed or
    triggers a fallback" -- the fallback-to-"insufficient information" case
    is this function's caller's responsibility (`agents.answer.node`), not
    this function's; this one only ever removes.
    """
    if not sentences or not chunks:
        return []

    clean_sentences = [strip_markers(sentence) for sentence in sentences]
    chunk_texts = [chunk.content for chunk in chunks]

    sentence_embeddings = await embedding.embed_texts(clean_sentences)
    chunk_embeddings = await embedding.embed_texts(chunk_texts)

    grounded: list[str] = []
    for original, clean, sentence_embedding in zip(
        sentences, clean_sentences, sentence_embeddings, strict=True
    ):
        if not clean:
            continue
        max_similarity = max(
            _cosine_similarity(sentence_embedding, chunk_embedding)
            for chunk_embedding in chunk_embeddings
        )
        if max_similarity >= _GROUNDED_THRESHOLD:
            grounded.append(original)
        elif max_similarity <= _UNGROUNDED_THRESHOLD:
            logger.info(
                "answer_agent_sentence_ungrounded", sentence=clean, similarity=max_similarity
            )
        else:
            is_grounded = await _llm_grounding_check(llm, clean, chunk_texts)
            if is_grounded:
                grounded.append(original)
            else:
                logger.info(
                    "answer_agent_sentence_ungrounded_by_llm",
                    sentence=clean,
                    similarity=max_similarity,
                )

    return grounded


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Dot product of two already-L2-normalized vectors == cosine
    similarity (`retrieval.embedding`'s `normalize_embeddings=True`).
    """
    return sum(x * y for x, y in zip(a, b, strict=True))


async def _llm_grounding_check(llm: BaseChatModel, sentence: str, chunk_texts: list[str]) -> bool:
    """Escalation path for ambiguous-similarity sentences (PROJECT_PLAN.md
    section 5.7): a single, targeted yes/no LLM call -- not a second full
    generation pass.
    """
    context = "\n\n".join(chunk_texts)
    prompt = (
        f"Context:\n{context}\n\n"
        f"Claim: {sentence}\n\n"
        "Is this claim directly supported by the context above? Answer with "
        "exactly one word: yes or no."
    )
    response = await llm.ainvoke(prompt)
    return str(response.content).strip().lower().startswith("y")
