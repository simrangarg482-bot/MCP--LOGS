"""Evidence-sufficiency verification -- runs BEFORE generation, distinct
from `agents.answer.grounding.verify_grounding`, which only runs AFTER.

THE GAP THIS CLOSES
    Investigation of the confidence-gated routing evaluation
    (`scripts/eval_confidence.py`, `scripts/eval_embedding_models.py`) found
    a 0.25 confidently-wrong-answer rate on `ambiguous` golden questions:
    ones where retrieval genuinely finds topically-relevant evidence that
    does not actually state the specific fact asked. Tracing the full flow
    (retrieval -> reranking -> context assembly -> `generate_answer` ->
    `verify_grounding` -> confidence) against the two real failing cases
    (`tensorflow-keras-upgrade-reason`, `python-version-conflict` in
    `scripts/eval_confidence_dataset.json`) found the SAME root cause behind
    both:

    `verify_grounding` re-embeds each SENTENCE THE MODEL ALREADY WROTE and
    checks it against the retrieved chunks -- it verifies the sentence's
    *wording* is textually supported by *some* chunk, never whether that
    chunk (or the assembled evidence as a whole) actually answers the
    *question*. Two distinct failure shapes both slip through this gap:

      1. Cross-chunk conflict: multiple chunks each state a genuinely real,
         individually-grounded fact, but the facts *disagree* (e.g. several
         commits pinning a Python project to 3.10.13, 3.11, 3.11.9, and 3.13
         at different points). The model picks one, states it as the
         answer, and that sentence passes grounding -- it really is
         supported by *a* chunk -- while ignoring that other equally-
         relevant chunks in the same context contradict it.
      2. Topic-adjacent borrowing: the chunk that actually answers the
         question in question is a bare fact (or a bare commit title with
         no body), but a DIFFERENT, superficially-similar chunk in the same
         context contains real, specific, textually-groundable detail about
         a *different* fact. The model launders that unrelated detail into
         an answer for the actual question; the sentence is grounded (the
         detail really is in a chunk) but it does not actually answer what
         was asked.

    Both cases produce a sentence that is honestly, correctly grounded by
    `verify_grounding`'s own definition of grounding -- because grounding
    only ever asks "is this text supported by the evidence," never "does
    the evidence, taken as a whole, actually answer the question."

THE FIX
    `assess_sufficiency` runs once per answer attempt, comparing the
    ORIGINAL QUESTION (not a generated sentence) against the full assembled
    context, and classifies it into exactly the three states this
    distinction requires:
      - "sufficient"   -- some evidence states a specific, direct answer,
                          and nothing else in the context contradicts it.
      - "partial"      -- the evidence is about the right topic/entity but
                          doesn't state the specific fact requested, OR
                          different evidence items disagree on it.
      - "insufficient" -- the evidence has no real bearing on the question.

    Wired into `agents.answer.node._generate_and_verify` as the FIRST step,
    before `generate_answer` is even called: anything other than
    "sufficient" raises the same `_UngroundedAnswerError` a failed
    post-generation grounding check already raises, so it flows through the
    exact same retry-then-decline path that already exists -- no new
    control flow, no new `AskResponse` shape, no change to
    `agents.confidence` or `Settings.confidence_threshold`. The smallest
    change that closes the gap: one new pre-generation check, reusing every
    piece of existing failure handling.
"""

from __future__ import annotations

from typing import Literal

from langchain_core.language_models import BaseChatModel

from app.agents.answer.generation import build_context_block
from app.retrieval.schemas import ScoredChunk
from app.shared.config.logging import get_logger

logger = get_logger(__name__)

SufficiencyVerdict = Literal["sufficient", "partial", "insufficient"]

_VALID_VERDICTS: tuple[SufficiencyVerdict, ...] = ("sufficient", "partial", "insufficient")

_PROMPT_TEMPLATE = """You are checking whether the evidence below is enough to answer a question with a \
specific, correct, non-conflicting fact. Do NOT answer the question yourself -- only judge the evidence.

Evidence:
{context_block}

Question: {query}

Work through this before deciding:
1. What is the ONE specific fact/value this question is actually asking for?
2. Does ANY evidence item state that specific fact/value directly (not just mention the same general \
topic, incident, commit, or entity)?
3. Go through EVERY numbered evidence item ONE BY ONE, in order, from [1] to the last one -- do not skip \
any and do not stop once you've found the two or three clearest matches. For each one, decide: does it \
touch the same setting/version/config/quantity this question is about, even if worded completely \
differently (a change title alone counts, e.g. "lock X to version A" and "update X to version B" are \
both about the same setting even though neither says "the answer is"). List every item that qualifies. \
Then compare the values across that full list: do any two of them give a DIFFERENT value/answer (e.g. \
different version numbers, different reasons, different timeframes)? If so, that is a conflict, even if \
one value looks newer, more specific, or more likely correct than the others -- the evidence itself does \
not state which one is authoritative, so you may not decide that for it.

Then classify into exactly one of:
SUFFICIENT -- step 2 is yes, AND step 3 found no conflict.
PARTIAL -- step 2 is no (evidence is on-topic but silent on the specific fact), OR step 3 found a conflict.
INSUFFICIENT -- the evidence has no real bearing on the question at all.

Respond with your brief reasoning for steps 1-3, then end your response with a final line in exactly this \
format (no other text on that line):
VERDICT: SUFFICIENT
or
VERDICT: PARTIAL
or
VERDICT: INSUFFICIENT"""


async def assess_sufficiency(
    llm: BaseChatModel, query: str, chunks: list[ScoredChunk]
) -> SufficiencyVerdict:
    """Classify whether `chunks`, as a whole, actually answers `query` --
    see module docstring for why this is a different question than
    `verify_grounding` asks, and why answering it requires comparing the
    QUESTION against the evidence rather than a generated sentence against
    it.

    `chunks` must be non-empty -- callers with zero retrieved chunks have
    nothing to assess and should never reach here (matching
    `agents.answer.node`'s existing zero-chunks guard).

    The prompt asks for brief step-by-step reasoning before a final
    `VERDICT: <word>` line, rather than a bare one-word response (unlike
    `agents.answer.grounding._llm_grounding_check`'s simpler yes/no check):
    reliably catching a cross-chunk conflict (two evidence items giving
    different values for the same fact) requires the model to actually
    compare evidence items against each other before committing to an
    answer -- a bare single-word response, tested directly against this
    module's own motivating failure case, measurably missed conflicts a
    reasoning-then-verdict response caught.

    Fails closed on a malformed/unparseable LLM response (defaults to
    "partial", not "sufficient"): this check exists specifically to catch
    confidently-wrong answers, so an ambiguous classifier response should
    err toward caution, the same posture `tests/rag_validation/README.md`
    already documents for this whole system ("a confidently wrong answer
    is the single most damaging failure mode").
    """
    context_block = build_context_block(chunks)
    prompt = _PROMPT_TEMPLATE.format(context_block=context_block, query=query)
    response = await llm.ainvoke(prompt)
    raw = str(response.content).strip()

    for line in reversed(raw.splitlines()):
        line_lower = line.strip().lower()
        if line_lower.startswith("verdict:"):
            candidate = line_lower.removeprefix("verdict:").strip()
            for verdict in _VALID_VERDICTS:
                if candidate.startswith(verdict):
                    return verdict
            break

    logger.warning("answer_agent_sufficiency_unparseable", raw_response=raw[:300])
    return "partial"
