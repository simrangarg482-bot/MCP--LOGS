"""app.evaluation.semantic -- Tier 3: the live, real-model semantic
benchmark and threshold-calibration layer.

Separate from Tier 1 (`app.evaluation`'s deterministic `EvaluationRunner`,
PR-blocking, no API key, no live corpus) by design -- see `schemas.py`'s
module docstring for exactly why the two vocabularies must not blur.
Distinct also from the two live harnesses that already existed before this
priority and are reused, not duplicated, by it:

  `scripts/eval_confidence.py`   -- confidence-threshold precision/recall
                                     sweep + answer-grounding rate, against
                                     real ingested `test-org` data.
  `tests/rag_validation/`        -- real end-to-end retrieval/grounding/
                                     citation PASS/FAIL validation, with an
                                     LLM judge kept deliberately separate
                                     from EKIP's own `verify_grounding`.

This package adds exactly what those two do not cover: a structured,
multi-dimension answer-quality rubric (correctness/relevance/usefulness/
faithfulness, not a single opaque score), and the Investigation Agent
baseline-vs-reflection A/B benchmark (which did not exist anywhere before
this priority). `calibration.py` formalizes the "provisional vs calibrated
vs insufficient data" status vocabulary this whole package -- and,
retroactively, `eval_confidence.py`'s own long-standing threshold work --
reports honestly.

See `docs/SEMANTIC_BENCHMARK.md` for the full architecture and
`scripts/run_semantic_evaluation.py` for the live execution entry point.
"""
