"""core/proactive -- deterministic, evidence-backed proactive pattern
detection over existing EKIP entities (Priority 6).

This is NOT an autonomous agent, NOT an anomaly-detection/ML system, and
NOT a second graph. Two deterministic detectors read real, structured
source data (`incidents.severity`/`project_id`, and the Priority 5 graph's
stored `documents` relationship) and produce evidence-backed
`ProactiveFinding` rows in one new table pair
(`proactive_findings`/`proactive_finding_evidence`, see
`app.database.models.pattern_models`). No LLM call, no paid API, and no
outbound notification exists anywhere in this module.

Module layout, same convention as every other `core/*` submodule:
    contract.py   -- the finding-type vocabulary; the single authority on
                      which finding types are legal, their evidence-role
                      vocabulary, thresholds, and scope.
    schemas.py    -- Pydantic contracts (`CandidateFinding`,
                      `ProactiveFinding`, `FindingDetail`, ...).
    repository.py -- pure data access on `proactive_findings`/
                      `proactive_finding_evidence`.
    service.py    -- the two deterministic detectors, detection
                      orchestration (upsert + reconcile), authorized
                      finding/evidence resolution, and the
                      lifecycle-cleanup hook other modules call.

See `docs/PROACTIVE_INTELLIGENCE.md` for the full architecture, the
authorization model, and what this module deliberately does not do.
"""
