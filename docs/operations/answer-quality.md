# Answer quality and latency gates

The production answer path is gated by the versioned controlled corpus in
`app/evaluation/fixtures/answer_quality_e2e_v1.json`. Grounded cases name the
exact documents that must be retrieved and cited; the negative control must
abstain. Keep corpus questions and documents together so evaluation cannot run
against unrelated live repository content.

Run the deterministic corpus contract:

```powershell
.venv/Scripts/pytest.exe tests/evaluation/test_answer_quality_corpus.py -q
```

Before release, seed these controlled documents into a non-production tenant
and run the semantic evaluation. Do not accept regressions in Recall@K,
citation validity, grounding, or abstention. Track p50 and p95 `/ask` latency
separately from answer quality.

The answer path embeds once, performs one dense and one lexical database
round-trip across all collections, fuses those global lists, and reranks 24
candidates down to 12. Clear questions avoid an LLM rewrite call; common
enterprise abbreviations are expanded deterministically. Vague questions and
incident-context questions may still use the LLM rewrite path.

The fixed MS-MARCO reranker's raw values are logits, not probabilities.
Confidence calibration is centered at the empirically observed borderline
score of `-8` with temperature `2`; a raw sigmoid incorrectly mapped useful
evidence to almost zero. After calibration, the live environment-variable
case routed to `answer` at `0.610` confidence and returned one verified
citation. Re-run the controlled corpus before changing these calibration
constants.

Live connector checks are credential-gated under
`scripts/live_connector_tests`. Missing credentials are skips, not passes. A
connector is production-approved only after authentication, fetch,
pagination/incremental behavior, normalization, and a full
ingestion-to-retrieval path pass against a non-production tenant.
