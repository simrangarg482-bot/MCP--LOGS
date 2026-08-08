"""Phase 6 -- End-to-End Test / master orchestrator.

PURPOSE
    Run the complete real-world ingestion + retrieval + RAG validation in
    one command:

        1. Validate environment
        2. Test connectors                  (test_connectors.py)
        3. Run ingestion                    (test_ingestion_pipeline.py)
        4. Verify vector storage            (part of step 3's own checks)
        5. Ask test questions               (test_end_to_end_rag.py)
        6. Evaluate responses               (part of step 5, LLM-as-judge)
        7. Generate report                  (this script's own final block)

WHY THIS IMPORTS THE OTHER SCRIPTS DIRECTLY, NOT VIA SUBPROCESS
    None of this directory's filenames start with a digit, so (unlike the
    sibling `scripts/realworld_onboarding/99_master_e2e.py`, which had to
    use `importlib.util` for that reason) these import as ordinary Python
    modules. Each phase script's own `main()` calls `utils.reset_results()`
    at its start -- this orchestrator captures each phase's detailed
    results via `utils.get_results()` immediately after that phase's
    `main()` returns and before the next phase's `reset_results()` call
    would erase them.

STOPS ON FAILURE BY DEFAULT
    If connectors testing finds zero usable connectors, or ingestion fails
    outright, later phases are skipped (there would be nothing meaningful to
    retrieve). Pass --continue-on-failure to run every phase regardless.

RUN
    python tests/ingestion_retrieval/run_rag_validation.py
    python tests/ingestion_retrieval/run_rag_validation.py --continue-on-failure
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import utils  # noqa: E402
from app.shared.config.settings import get_settings  # noqa: E402


def _validate_environment() -> bool:
    print("### 1. Validate environment")
    ok = True
    try:
        settings = get_settings()
        print(f"  DATABASE_URL configured: {'yes' if settings.database_url else 'no'}")
        print(f"  OPENAI_API_KEY configured: {'yes' if settings.openai_api_key else 'no'}")
        if not settings.openai_api_key:
            print("  WARNING: no OPENAI_API_KEY -- retrieval/RAG phases will fail at the LLM call step.")
            ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED to load project settings: {exc}")
        ok = False
    return ok


def main() -> bool:
    continue_on_failure = "--continue-on-failure" in sys.argv
    all_step_lines: list[tuple[str, bool]] = []

    env_ok = _validate_environment()
    all_step_lines.append(("Environment", env_ok))
    if not env_ok and not continue_on_failure:
        return _final_report(all_step_lines)

    print("\n### 2. Test connectors")
    import test_connectors

    connectors_ok = test_connectors.main()
    connector_results = utils.get_results()
    for result in connector_results:
        if result.name.endswith(": connector"):
            label = result.name.split(":")[0].replace("_", " ").title() + " Connector"
            all_step_lines.append((label, result.passed))
    if not connectors_ok and not continue_on_failure:
        return _final_report(all_step_lines)

    print("\n### 3-4. Run ingestion + verify vector storage")
    import test_ingestion_pipeline

    ingestion_ok = test_ingestion_pipeline.main()
    ingestion_results = {r.name: r for r in utils.get_results()}
    job_ok = ingestion_results.get("run_ingestion_job")
    chunks_ok = ingestion_results.get("chunks stored and retrievable")
    all_step_lines.append(("Documents Ingested", bool(job_ok and job_ok.passed)))
    all_step_lines.append(("Chunks Generated", bool(chunks_ok and chunks_ok.passed)))
    all_step_lines.append(("Embeddings Created", bool(chunks_ok and chunks_ok.passed)))
    all_step_lines.append(("Vector Storage", bool(chunks_ok and chunks_ok.passed)))
    if not ingestion_ok and not continue_on_failure:
        return _final_report(all_step_lines)

    print("\n### 5-6. Ask test questions + evaluate responses")
    import test_end_to_end_rag

    rag_ok = test_end_to_end_rag.main()
    rag_results = utils.get_results()
    ran_any = len(rag_results) > 0
    all_step_lines.append(("Retrieval", ran_any))
    all_step_lines.append(("Answer Generation", ran_any))
    all_step_lines.append(("Answer Evaluation", rag_ok))

    return _final_report(all_step_lines)


def _final_report(step_lines: list[tuple[str, bool]]) -> bool:
    print("\n" + "=" * 50)
    print("EKIP RAG PIPELINE VALIDATION")
    print("=" * 50)
    overall = True
    for label, passed in step_lines:
        overall = overall and passed
        print(f"{label:<28} {'PASS' if passed else 'FAIL'}")
    print()
    print(f"Overall Result: {'PASS' if overall else 'FAIL'}")
    print("=" * 50)

    utils.dispose_shared_loop()
    return overall


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
