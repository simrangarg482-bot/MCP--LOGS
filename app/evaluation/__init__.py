"""EKIP evaluation harness (Priority 2 of the production-maturity roadmap;
see `docs/PROJECT_STATUS.md`'s Phase 16 entry and `EKIP_STRATEGIC_ANALYSIS.md`
section 2.2 for why this exists).

Owned by: this package. A standalone measurement layer, not a runtime
dependency of `app.api`/`app.agents`/etc. -- nothing outside `app.evaluation`
and `scripts/run_evaluation.py` imports from here, so it carries no
import-linter contract of its own (see `pyproject.toml`'s `[tool.importlinter]`
section: none of the existing "forbidden" contracts name `app.evaluation` as
either a source or a forbidden module, and none needs to).

**Complements, does not replace, two pre-existing live-only harnesses:**
- `scripts/eval_confidence.py` -- confidence-threshold sweep (precision/
  recall/F1 on the answer-vs-investigate routing decision) against a real,
  already-ingested organization and a funded `OPENAI_API_KEY`.
- `tests/rag_validation/run_validation.py` -- grounded/negative-control
  question grading (retrieval/answer/grounding/citations) against the same
  kind of real corpus.

Both are excellent at what they do and are left completely untouched. What
neither can do -- and what this package adds -- is run **without** a live
database or a paid API key: rank-aware retrieval metrics (Recall@K,
Precision@K, MRR) against a small fixture corpus, deterministic grounding/
citation checks, generic reusable answer assertions, an initial investigation
evaluator, and confidence-calibration bucket analysis. The calibration
analyzer can also consume `scripts/eval_confidence.py`'s own JSON report
directly (`adapters/eval_confidence_report.py`) -- real production
measurements, zero duplicated live-pipeline code.

Package layout:
    schemas.py       -- EvaluationCase, DatasetMetadata, EvaluationResult,
                         MetricResult, mode/category enums.
    datasets/         -- JSONL case loading + validation.
    metrics/          -- pure, independently-tested scoring functions
                         (retrieval, grounding, confidence, investigation).
    assertions/       -- generic answer-text assertion types.
    adapters/         -- pluggable "system under test" seams (retrieval,
                         semantic similarity, LLM judge, and a reader for
                         `eval_confidence.py`'s own report format).
    reporting/        -- console + JSON report rendering.
    fixtures/         -- the small, versioned regression datasets + the
                         synthetic corpus the deterministic retrieval
                         adapter serves.
    runner.py         -- wires the above into `EvaluationRunner`.

See `scripts/run_evaluation.py` for the CLI entry point.
"""
