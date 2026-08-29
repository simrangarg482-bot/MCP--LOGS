"""Structured, mode-aware answer-quality evaluator -- Tier 3.

Not a single opaque "score this 1-10" call, and (Priority 9's fix) not a
single rubric applied to every answer regardless of what kind of answer it
is. `judge_answer_quality` runs the deterministic answer-mode check first
(`outcome.is_refusal_text`), then routes to exactly one of two rubrics --
see `schemas.SubstantiveAnswerJudgement`/`schemas.RefusalJudgement` for what
each scores and why they're different dimensions, not the same four
reused. One LLM call either way (section 7: no unnecessary calls; section
12: prefer a single mode-aware judge call over redundant ones) --
`SubstantiveAnswerJudgement.observed_mode` is classified by the SAME call
that scores the substantive dimensions, not a separate classification call.

SELF-JUDGING CONTROLS (Priority 8 section 5, unchanged by this priority)
    This evaluator uses the same `get_llm()`/model as the system under
    test -- this codebase has exactly one configured LLM provider. The
    methodology controls already in place:

      1. `faithfulness` (substantive rubric) is graded against
         `evidence_texts` ONLY -- a claim absent from the evidence cannot
         score as faithful regardless of whether it happens to be true,
         the same discipline `agents.answer.grounding.verify_grounding`
         already applies in production.
      2. `correctness` prefers `reference_answer` when the case supplies
         one, over the evaluator's own judgment.
      3. Every score carries a required `reason` string.
      4. `unsupported_claim_avoidance` (refusal rubric) applies the same
         evidence-only discipline to a REFUSAL's own explanation text --
         a refusal that fabricates a specific reason ("the logs were
         corrupted") not actually present in the evidence is not faithful
         either, and this priority's rubric is built to catch that, not
         only to reward the presence of a refusal.
      5. Limitation stated honestly, not hidden: this IS the same model
         family generating and grading in the fully live case. See
         `docs/SEMANTIC_BENCHMARK.md`'s "Evaluator limitations" section.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.language_models import BaseChatModel
from pydantic import ValidationError

from app.agents.prompt_safety import build_messages
from app.evaluation.semantic.outcome import is_refusal_text
from app.evaluation.semantic.schemas import (
    AnswerJudgement,
    AnswerQualityCase,
    ObservedAnswerMode,
    RefusalJudgement,
    SubstantiveAnswerJudgement,
)
from app.shared.config.logging import get_logger

logger = get_logger(__name__)


class AnswerQualityParsingError(Exception):
    """Raised when the evaluator's response isn't valid, expected-shape
    JSON, or fails schema validation -- callers must surface this as an
    evaluator failure (`AnswerQualityResult.error`), never silently treat
    it as a passing or default score."""


def _evidence_block(case: AnswerQualityCase) -> str:
    return (
        "\n\n".join(f"- {text}" for text in case.evidence_texts)
        if case.evidence_texts
        else "(no evidence was supplied for this case)"
    )


def _build_substantive_prompt(
    case: AnswerQualityCase, generated_answer: str
) -> tuple[str, str, str]:
    reference_note = (
        f"\n\nA human-authored reference answer is available -- prefer it over your own "
        f"judgment of correctness: {case.reference_answer!r}"
        if case.reference_answer
        else ""
    )
    system_instructions = (
        "You are grading one generated answer against the evidence it was supposed to be "
        "based on. This answer is NOT a refusal -- it states something substantive. First "
        "classify `observed_mode`:\n"
        '  "substantive_answer" -- the answer states its claims directly, without '
        "meaningfully flagging uncertainty, gaps, or conflicting evidence.\n"
        '  "qualified_answer" -- the answer explicitly communicates uncertainty, a '
        'limitation, or incomplete evidence coverage (e.g. "based on one source, it '
        'appears...", "I can\'t confirm X, but Y indicates...") rather than stating '
        "everything with full confidence.\n\n"
        "Then score four INDEPENDENT dimensions, each 0.0-1.0, each with a short reason:\n\n"
        "correctness  -- is the answer factually accurate relative to the evidence"
        f"{' and the reference answer' if case.reference_answer else ''}?\n"
        "relevance    -- does the answer actually address the question asked?\n"
        "usefulness   -- would this answer help someone resolve the underlying question?\n"
        "faithfulness -- is EVERY claim in the answer traceable to the evidence below? A "
        "true-but-unsupported claim must NOT score as faithful -- faithfulness measures "
        "grounding in the given evidence, not real-world accuracy. If the evidence "
        "conflicts and the answer states one value with full confidence anyway, that is "
        "NOT faithful -- classify it as `observed_mode: substantive_answer` and score "
        "faithfulness low, rather than crediting it for citing a real (if disputed) chunk.\n\n"
        "Respond with ONLY a single JSON object (no markdown code fences, no commentary) "
        "with exactly this shape:\n"
        '{"observed_mode": "substantive_answer" | "qualified_answer", '
        '"correctness": {"score": 0.0, "reason": "..."}, '
        '"relevance": {"score": 0.0, "reason": "..."}, '
        '"usefulness": {"score": 0.0, "reason": "..."}, '
        '"faithfulness": {"score": 0.0, "reason": "..."}}\n\n'
        "Every `score` must be a number from 0.0 to 1.0. Every `reason` must be non-empty."
        f"{reference_note}"
    )
    task = f"Question: {case.question}\n\nGenerated answer: {generated_answer}"
    return system_instructions, _evidence_block(case), task


def _build_refusal_prompt(case: AnswerQualityCase, generated_answer: str) -> tuple[str, str, str]:
    system_instructions = (
        "You are grading one REFUSAL -- the system declined to answer substantively. A "
        "refusal does not automatically deserve full credit merely for existing: your job "
        "is to tell a CORRECT abstention (the evidence genuinely did not support an answer) "
        "from a LAZY one (the evidence actually did answer the question, but the system "
        "declined anyway). Score four INDEPENDENT dimensions, each 0.0-1.0, each with a "
        "short reason:\n\n"
        "abstention_correctness      -- was declining actually justified? Read the evidence "
        "below and decide whether it genuinely lacks a specific, direct answer to the "
        "question. If the evidence clearly states an answer and the system still refused, "
        "score this LOW -- that is an incorrect, lazy refusal, not a safe one.\n"
        "unsupported_claim_avoidance -- does the refusal's own explanation avoid inventing "
        "any specific fact, reason, or detail not actually present in the evidence below? "
        "A refusal that fabricates a plausible-sounding reason for why it can't answer is "
        "not faithful either.\n"
        "explanation_quality         -- is the explanation clear and honest about what is/"
        "isn't known, without implying evidence exists that doesn't?\n"
        "appropriate_next_step       -- where appropriate, does it suggest a useful way to "
        "obtain the missing information, without fabricating an answer just to avoid saying "
        '"I don\'t know"? Score this leniently if no next step is applicable to the '
        "question -- absence of a next step is not itself a flaw.\n\n"
        "Respond with ONLY a single JSON object (no markdown code fences, no commentary) "
        "with exactly this shape:\n"
        '{"abstention_correctness": {"score": 0.0, "reason": "..."}, '
        '"unsupported_claim_avoidance": {"score": 0.0, "reason": "..."}, '
        '"explanation_quality": {"score": 0.0, "reason": "..."}, '
        '"appropriate_next_step": {"score": 0.0, "reason": "..."}}\n\n'
        "Every `score` must be a number from 0.0 to 1.0. Every `reason` must be non-empty."
    )
    task = f"Question: {case.question}\n\nRefusal text: {generated_answer}"
    return system_instructions, _evidence_block(case), task


async def judge_answer_quality(
    llm: BaseChatModel,
    case: AnswerQualityCase,
    generated_answer: str,
    *,
    known_mode: ObservedAnswerMode | None = None,
) -> AnswerJudgement:
    """Deterministically routes to the substantive or refusal rubric (see
    module docstring), then makes exactly one LLM call. Raises
    `AnswerQualityParsingError` on any malformed or out-of-range output --
    callers should run this through `agents.retry.call_with_retry` the same
    way every other JSON-prompt LLM call in this codebase does, and treat
    exhaustion as an evaluator failure, never a fabricated judgement.

    `known_mode` (Priority 10) is the mode-detection hierarchy's TIER 1:
    when the caller already has the production pipeline's own authoritative
    outcome (`agents.answer.node.AnswerOutcome.mode`, via `runner.py`
    calling `generate_answer_with_outcome` for a live-generated case), pass
    it here to skip TIER 3's legacy sentinel matching (`is_refusal_text`)
    entirely -- this is what fixes the `aq-partial-evidence` bug: a
    free-text decline that doesn't match either legacy sentinel is still
    correctly routed to the refusal rubric, because the caller already
    KNOWS it's a refusal from the pipeline's own sufficiency/grounding
    decision, not from this function guessing at the text. Only ever
    `"no_answer"` in practice -- production has no machine-readable
    "answered but only substantively/qualified" distinction to pass through
    as `known_mode="substantive_answer"`/`"qualified_answer"` (see
    `AskResponse.answer_mode`'s own docstring), so that distinction still
    always goes through TIER 4 (the semantic judge call below) regardless
    of `known_mode`. `None` (the default) preserves every existing caller's
    behavior byte-for-byte: falls through to TIER 3 exactly as before this
    parameter existed.
    """
    if known_mode == "no_answer" or (known_mode is None and is_refusal_text(generated_answer)):
        system_instructions, evidence_block, task = _build_refusal_prompt(case, generated_answer)
        messages = build_messages(
            system_instructions=system_instructions, evidence_block=evidence_block, task=task
        )
        response = await llm.ainvoke(messages)
        parsed = _parse_response(str(response.content).strip(), context="refusal")
        try:
            refusal = RefusalJudgement.model_validate(parsed)
        except ValidationError as exc:
            raise AnswerQualityParsingError(
                f"refusal evaluator response failed schema validation: {exc}"
            ) from exc
        return AnswerJudgement(observed_answer_mode="no_answer", refusal=refusal)

    system_instructions, evidence_block, task = _build_substantive_prompt(case, generated_answer)
    messages = build_messages(
        system_instructions=system_instructions, evidence_block=evidence_block, task=task
    )
    response = await llm.ainvoke(messages)
    parsed = _parse_response(str(response.content).strip(), context="substantive")
    try:
        substantive = SubstantiveAnswerJudgement.model_validate(parsed)
    except ValidationError as exc:
        raise AnswerQualityParsingError(
            f"substantive evaluator response failed schema validation: {exc}"
        ) from exc
    return AnswerJudgement(observed_answer_mode=substantive.observed_mode, substantive=substantive)


def _parse_response(raw_text: str, *, context: str) -> dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[len("json") :].strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(f"answer_quality_{context}_parse_failed", error=str(exc))
        raise AnswerQualityParsingError(
            f"{context} evaluator response was not valid JSON: {raw_text[:200]!r}"
        ) from exc
    if not isinstance(parsed, dict):
        logger.warning(f"answer_quality_{context}_unexpected_shape", raw_type=type(parsed).__name__)
        raise AnswerQualityParsingError(
            f"{context} evaluator response was not a JSON object: {raw_text[:200]!r}"
        )
    return parsed
