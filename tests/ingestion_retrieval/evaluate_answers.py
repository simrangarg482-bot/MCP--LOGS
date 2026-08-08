"""Phase 4 -- Answer Quality Evaluation.

PURPOSE
    Score a generated answer against its retrieved context on five axes:
    relevance, grounding, hallucination, citation correctness, completeness.

HOW THIS EVALUATOR WORKS -- AND WHY IT'S SEPARATE FROM EKIP'S OWN GROUNDING
CHECK
    EKIP's real production pipeline already has one grounding/hallucination
    check built in: `app.agents.answer.grounding.verify_grounding` runs
    automatically inside `answer_question`, stripping ungrounded sentences
    before an answer is ever returned. That tells you "does EKIP's own
    pipeline believe its answer is grounded" -- it cannot also serve as this
    task's independent, external quality check (Relevance/Grounding/
    Hallucination/Citation correctness/Completeness), since it would just be
    grading EKIP's homework with EKIP's own answer key.

    This evaluator is a genuinely separate LLM-as-judge, built the same way
    the rest of this harness treats infrastructure it doesn't want to
    reinvent: it calls EKIP's real, unmodified `app.agents.llm.get_llm()`
    (same OpenAI client/model the app itself uses, at `temperature=0` for
    judging determinism) with its OWN, distinct evaluation prompt -- not
    `verify_grounding`'s prompt, not the answer-generation prompt. This is
    the same "reuse real, working infrastructure; write new orchestration
    logic" pattern used everywhere else in this harness (e.g. reusing
    `session_scope`/`get_llm` rather than hand-rolling a second DB/LLM
    client).

WHAT IT CALLS
    `app.agents.llm.get_llm()` -- real, unmodified, requires the same real
    `OPENAI_API_KEY` the rest of this harness's LLM-touching scripts need.

RUN STANDALONE (evaluates one example question end-to-end, for a quick
sanity check of the evaluator itself)
    python tests/ingestion_retrieval/evaluate_answers.py

USED BY
    test_end_to_end_rag.py, which calls `evaluate_answer(...)` once per
    question loaded from questions.json.

COMMON FAILURES
    - Judge LLM returns non-JSON / malformed JSON: this evaluator falls back
      to `overall = "FAIL"` with `parse_error` set in the result rather than
      crashing -- a judge-formatting failure is reported, not silently
      treated as a passing score.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.agents.llm import get_llm  # noqa: E402

_JUDGE_PROMPT_TEMPLATE = """You are an impartial evaluator grading a RAG system's answer. Judge STRICTLY \
based on the retrieved context provided below -- do not use outside knowledge of your own.

Question:
{question}

Retrieved Context (what the system had available to answer from):
{context}

Generated Answer:
{answer}

Citations Provided: {citations}

Score the answer on these five axes and respond with ONLY a single JSON object, no other text, using \
exactly these keys:
{{
  "relevance": <integer 1-10, does the answer address the question>,
  "grounded": "YES" or "NO" -- is the answer supported by the retrieved context above,
  "hallucination": "YES" or "NO" -- does the answer contain claims NOT present in the retrieved context,
  "citation_accuracy": <integer 1-10, do the citations actually support the claims they're attached to>,
  "completeness": <integer 1-10, did the answer address every part of the question>,
  "reasoning": "<one or two sentence justification>"
}}"""


def evaluate_answer(question: str, answer: str, context_chunks: list[str], citations: list) -> dict:
    """Returns a dict with relevance/grounded/hallucination/citation_accuracy/
    completeness/reasoning/overall ("PASS"/"FAIL"), plus raw_response for
    debugging. `context_chunks` should be the actual chunk content strings
    the answer was generated from (not just titles)."""
    llm = get_llm(temperature=0.0)
    context_block = "\n\n".join(f"[{i}] {c}" for i, c in enumerate(context_chunks, start=1)) or "(no context retrieved)"
    prompt = _JUDGE_PROMPT_TEMPLATE.format(
        question=question, context=context_block, answer=answer, citations=citations
    )

    raw_response = ""
    try:
        response = llm.invoke(prompt)
        raw_response = str(response.content)
        parsed = json.loads(_extract_json(raw_response))
    except Exception as exc:  # noqa: BLE001 - judge failure is itself a result to report
        return {
            "relevance": 0,
            "grounded": "NO",
            "hallucination": "YES",
            "citation_accuracy": 0,
            "completeness": 0,
            "reasoning": f"Judge call/parse failed: {exc}",
            "overall": "FAIL",
            "parse_error": str(exc),
            "raw_response": raw_response,
        }

    grounded = str(parsed.get("grounded", "NO")).upper() == "YES"
    hallucination = str(parsed.get("hallucination", "YES")).upper() == "YES"
    relevance = int(parsed.get("relevance", 0))
    citation_accuracy = int(parsed.get("citation_accuracy", 0))
    completeness = int(parsed.get("completeness", 0))

    overall = "PASS" if (grounded and not hallucination and relevance >= 6 and citation_accuracy >= 6) else "FAIL"

    return {
        "relevance": relevance,
        "grounded": "YES" if grounded else "NO",
        "hallucination": "YES" if hallucination else "NO",
        "citation_accuracy": citation_accuracy,
        "completeness": completeness,
        "reasoning": parsed.get("reasoning", ""),
        "overall": overall,
        "raw_response": raw_response,
    }


def _extract_json(text: str) -> str:
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in judge response: {text!r}")
    return text[start : end + 1]


def print_evaluation(question: str, result: dict) -> None:
    print(f"\nQuestion:\n{question}")
    print("\nAnswer Quality:\n")
    print(f"Relevance: {result['relevance']}/10")
    print(f"Grounded: {result['grounded']}")
    print(f"Hallucination: {result['hallucination']}")
    print(f"Citation Accuracy: {result['citation_accuracy']}/10")
    print(f"Completeness: {result['completeness']}/10")
    print(f"Reasoning: {result['reasoning']}")
    print(f"Overall: {result['overall']}")


def main() -> bool:
    # Standalone sanity check: run one question through the real
    # introspection pipeline (imported from test_retrieval_pipeline.py, not
    # duplicated) and evaluate the result.
    import uuid

    import config as harness_config
    import utils
    from test_retrieval_pipeline import _introspect

    cfg = harness_config.load_config()
    identity = utils.bootstrap_admin_sync(
        org_name=cfg.org_name, org_slug=cfg.org_slug, email=cfg.admin_email, display_name=cfg.admin_display_name
    )
    organization_id = uuid.UUID(identity["organization_id"])
    question = "Where is the authentication configuration documented?"
    trace = utils.run_async(_introspect(question, organization_id))

    result = evaluate_answer(question, trace["answer"], trace["context_texts"], trace["citations"])
    print_evaluation(question, result)
    return result["overall"] == "PASS"


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
