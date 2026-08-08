"""Phase 5 -- Automated Question Testing.

PURPOSE
    Load `questions.json`, run each question through EKIP's real retrieval +
    RAG pipeline (via the same internal-introspection helper
    `test_retrieval_pipeline._introspect` uses -- see that script's module
    docstring for exactly what is and isn't a REST call), evaluate each
    answer with `evaluate_answers.evaluate_answer`, and check whether the
    citations' actual source(s) overlap with `expected_sources`.

WHAT "expected_sources" CHECKING ACTUALLY MEANS HERE
    EKIP's `Citation` schema (`app/shared/schemas/agent_contracts.py`)
    carries `document_id`, not a source label directly -- so this script
    does one extra, real, read-only DB lookup per citation
    (`Document.source`, via `app.database.models.ingestion_models.Document`)
    to resolve what source each cited document actually came from, then
    compares that set against `expected_sources`. This is a best-effort,
    advisory check, not a hard pass/fail gate on its own: a question can
    still get a perfectly good, well-grounded answer sourced from data this
    harness didn't anticipate (e.g. `expected_sources` guessed "github" but
    the real answer came from a Slack thread that also covered it) --
    source-overlap mismatches are reported, not treated as failures by
    themselves. Only the evaluator's own PASS/FAIL (relevance/grounding/
    hallucination/citations) counts toward the real pass/fail total.

RUN
    python tests/ingestion_retrieval/test_end_to_end_rag.py

OUTPUT
    Per-question evaluation blocks (same format as evaluate_answers.py),
    plus a final report: how many questions passed evaluation, and a note
    on any source-overlap mismatches.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as harness_config  # noqa: E402
import utils  # noqa: E402
from evaluate_answers import evaluate_answer, print_evaluation  # noqa: E402
from test_retrieval_pipeline import _introspect  # noqa: E402

from sqlalchemy import select  # noqa: E402

from app.database.models.ingestion_models import Document  # noqa: E402
from app.database.session import session_scope  # noqa: E402

_QUESTIONS_PATH = Path(__file__).resolve().parent / "questions.json"


async def _resolve_citation_sources(document_ids: list[uuid.UUID]) -> set[str]:
    if not document_ids:
        return set()
    async with session_scope() as session:
        result = await session.execute(select(Document.source).where(Document.id.in_(document_ids)))
        return set(result.scalars().all())


def main() -> bool:
    utils.reset_results()
    cfg = harness_config.load_config()
    questions = json.loads(_QUESTIONS_PATH.read_text())

    identity = utils.bootstrap_admin_sync(
        org_name=cfg.org_name, org_slug=cfg.org_slug, email=cfg.admin_email, display_name=cfg.admin_display_name
    )
    organization_id = uuid.UUID(identity["organization_id"])

    source_mismatches = []
    for entry in questions:
        question = entry["question"]
        expected_sources = set(entry.get("expected_sources", []))

        try:
            trace = utils.run_async(_introspect(question, organization_id))
        except Exception as exc:  # noqa: BLE001
            print(f"\nQuestion:\n{question}\n--- FAILURE ---\nCould not run pipeline: {exc}")
            utils.record_result(f"e2e: {question[:40]}", False, detail=str(exc))
            continue

        result = evaluate_answer(question, trace["answer"], trace["context_texts"], trace["citations"])
        print_evaluation(question, result)
        utils.record_result(f"e2e: {question[:40]}", result["overall"] == "PASS", detail=result["reasoning"])

        cited_doc_ids = [doc_id for doc_id, _excerpt in trace["citations"]]
        actual_sources = utils.run_async(_resolve_citation_sources(cited_doc_ids))
        if expected_sources and actual_sources and not (expected_sources & actual_sources):
            source_mismatches.append((question, expected_sources, actual_sources))
            print(f"  (note: expected_sources={sorted(expected_sources)} but citations came from {sorted(actual_sources)})")

    if source_mismatches:
        print("\n--- Source-overlap notes (advisory only, does not affect PASS/FAIL) ---")
        for question, expected, actual in source_mismatches:
            print(f"  {question!r}: expected one of {sorted(expected)}, got {sorted(actual)}")

    return utils.print_summary(title="EKIP AUTOMATED QUESTION TESTING -- SUMMARY")


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
