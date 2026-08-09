# Real end-to-end RAG validation

Runs realistic questions through EKIP's **real, unmodified** RAG pipeline against
the **real data its connectors already ingested**, and reports PASS/FAIL per
question.

```
question → query rewrite → hybrid retrieval → cross-encoder reranking
         → context assembly → LLM answer → citations
```

Nothing in the pipeline is stubbed or re-implemented. This directory only adds
orchestration and grading:

| File | Purpose |
|---|---|
| `rag_dataset.json` | The question set: grounded questions + negative controls, with expected criteria |
| `run_validation.py` | The runner — drives the real pipeline, grades, prints the report |

## Running it

```bash
python tests/rag_validation/run_validation.py                     # everything
python tests/rag_validation/run_validation.py --only negative     # just the controls
python tests/rag_validation/run_validation.py --limit 3           # quick smoke run
python tests/rag_validation/run_validation.py --org-slug test-org # pick the org
```

Requires a live `DATABASE_URL` and a real `OPENAI_API_KEY` in the project's root
`.env`, and an organization that actually has ingested documents. Exit code is
`0` only if every question passes.

This is a **script, not a pytest test**, on purpose: `pyproject.toml` sets
`testpaths = ["tests"]`, so a `test_*.py` here would be pulled into the ordinary
unit-test suite, which must stay fast, offline, and free. The filename keeps
pytest from collecting it.

### It will not silently "pass" against an empty org

The runner resolves the organization by slug, counts its live documents, and
**aborts** if that count is zero — otherwise every question would trivially
report "retrieved nothing" and the run would look like a pipeline failure when
it is really a configuration mistake. `--org-slug` must point at the org your
connectors actually ingested into.

## Which pipeline is exercised, and why two of them

Each question is run through **both**:

1. **The component pipeline directly** — `rewrite_query` → `retrieval.service.search`
   → `rerank` → `assemble_context` → `generate_answer` → `build_citations`.
   This is what gives full visibility into retrieval and reranking, so the report
   can show which sources were actually retrieved and grade grounding against the
   exact context the answer was generated from.

2. **The production entry point** — `agents.service.answer_question`, the same
   call a REST `POST /ask` makes, including the **confidence gate and
   answer/investigation routing**.

The second one matters specifically for the negative controls. The direct
component path always generates *something* once any context exists (nearest
neighbours always come back), so on its own it cannot tell you whether the system
would actually have *served* that answer to a user. The confidence gate can.

## Grading

### Grounded questions — all four checks must pass

| Check | What it proves |
|---|---|
| `[RETRIEVAL]` | At least one `must_retrieve_any` string appears in the assembled context — relevant evidence was really retrieved, rather than the answer coming from the LLM's own parametric memory. |
| `[ANSWER]` | Every `expected_fact_groups` group is satisfied. Each group is a set of acceptable phrasings for **one** fact, so paraphrase is fine but the fact itself is required — this is what "the answer actually answers the question" means here. |
| `[GROUNDING]` | Deterministic: no expected fact appears in the answer that is **absent from the retrieved context**. Plus an independent LLM judge returning `grounded=YES` and `hallucination=NO`. |
| `[CITATIONS]` | At least one citation, and at least one whose **excerpt is genuinely found in the assembled context** — i.e. citations point at evidence really retrieved, not at fabricated references. |

`[sources]` is reported but **advisory** — it never fails a question. The same
incidents are discussed in both GitHub and Slack, so a perfectly good answer can
legitimately be cited from a source this dataset did not happen to predict.

### Negative controls — must not answer confidently

PASS means the system did **not** serve a confident, cited answer: it routed to
investigation, or returned no answer, or returned no citations, or fell below
`Settings.confidence_threshold`, or explicitly declined in the answer text.

FAIL means it produced a confident, citation-backed answer to a question the
corpus cannot support — a confidently incorrect answer, which is the single most
damaging failure mode for a system like this.

Two of the four controls (`neg-k8s-autoscaling`, `neg-aws-spend`) are
**domain-adjacent and plausible-sounding on purpose**. A question about Kubernetes
thresholds or AWS spend sounds like something this corpus *might* contain, so a
weak RAG system will happily confabulate a specific number. The obviously
out-of-domain control (`neg-world-cup`) is the easy case; those two are the real
test.

## Why the judge is separate from EKIP's own grounding check

EKIP already runs `app.agents.answer.grounding.verify_grounding` inside the
production answer path. That is exactly why it **cannot** serve as this harness's
grounding check — using it would be grading EKIP's homework with EKIP's own answer
key. The judge here is a separate prompt at `temperature=0`, and it is
deliberately backed by the deterministic fact/context checks above so that one
flaky judge call cannot silently pass a bad answer.

## Dataset

Every `grounded` question is answerable **only** from content the real GitHub and
Slack connectors ingested (plus one human-published knowledge document). The
`must_retrieve_any` and `expected_fact_groups` strings are taken verbatim from
that ingested content — they are not invented, and not guesses at what the model
"should probably say".

To extend it, add an entry to `rag_dataset.json`:

```jsonc
{
  "id": "short-stable-id",
  "kind": "grounded",                                  // or "negative"
  "question": "...",
  "must_retrieve_any": ["phrase from the real corpus"],
  "expected_fact_groups": [["fact", "paraphrase of it"], ["second required fact"]],
  "expected_sources": ["github", "slack"]              // Document.source values, advisory
}
```

`expected_sources` are `Document.source` values (`github`, `slack`, `manual`,
`jira`, …) — **not** retrieval collection names (`documentation`, `code`,
`conversations`). Those are different vocabularies and mixing them up makes the
check silently never match.

## Side effects

`answer_question` writes one `agent_executions` row per question — its normal
production behaviour, visible afterwards in `GET /observability/agents`. Nothing
else is written. No application code and no existing test is modified.
