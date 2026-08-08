# EKIP Ingestion & Retrieval RAG Test Harness

Tests EKIP's real ingestion and retrieval/RAG pipeline the way a real
enterprise customer would use it: real connector credentials, real
documents, real embeddings, real pgvector storage, real LLM-generated
answers with citations, and an independent LLM-as-judge quality check.

**This harness never modifies anything under `app/`.** Every file here is
new, standalone, and confined to `tests/ingestion_retrieval/`. Where EKIP
has no REST/MCP endpoint for something this task needs (triggering an
ingestion sync; bootstrapping the very first identity), this harness calls
the real, unmodified underlying Python function directly instead — every
such case is named explicitly in the relevant script's own docstring, not
silently routed around.

## Setup

```bash
cd tests/ingestion_retrieval
cp .env.example .env
# fill in whichever connector credentials you have -- any left blank are
# reported SKIPPED, never faked
```

Run everything from the **project's own virtualenv** (these scripts
`import app.*` directly):

```bash
pip install python-dotenv httpx   # if not already installed
python tests/ingestion_retrieval/run_rag_validation.py
```

The project's own API server must be running (`python scripts/run_api_server.py`
or however you normally start it) for the connector-registration REST call
and the `/ask` REST call to succeed. A real `DATABASE_URL` (with the
Postgres `vector` extension) and `OPENAI_API_KEY` must be set in the
**project's own** root `.env` — this harness reads those via
`app.shared.config.settings.get_settings()`, never redefines them.

## What actually exists in EKIP today (read directly from the code, not assumed)

| Requested in the task | Real status in this codebase |
|---|---|
| Slack, GitHub, SharePoint connectors | **Real, implemented.** `app/ingestion/connectors/{slack,github,sharepoint}.py` |
| Teams, Jira, Confluence, Azure DevOps connectors | **Real, implemented** (added in an earlier milestone) |
| Runbooks connector | **Real, implemented** — internal only, re-ingests this project's own approved postmortems, no external API |
| Google Drive connector | **Does not exist.** No file, no class, anywhere. `test_connectors.py` reports this explicitly rather than inventing one. |
| Chunking | **Real.** `app/ingestion/processors/chunking.py` — 2000-char max, no overlap, content-type-aware splitting |
| Embeddings | **Real, local model** — `sentence-transformers/all-MiniLM-L6-v2`, 384-dim, no API key needed |
| Vector DB | **pgvector only.** `app/retrieval/qdrant/` is an empty placeholder package; `default_vector_backend` setting is dead config, never read. |
| Reranking | **Real, local cross-encoder** — `cross-encoder/ms-marco-MiniLM-L-6-v2` |
| Query rewriting, context assembly, LLM answer generation, citations | **Real**, all in `app/agents/`; LLM is OpenAI `gpt-4o-mini` via `app/agents/llm.py` |
| Grounding/hallucination check | **Real, already built into production** — `app/agents/answer/grounding.py::verify_grounding` runs inside every `/ask` call automatically |
| A single "ask a question, get an answer" endpoint | **Real.** `POST /ask` and MCP tool `ask_question`, both call `app.agents.service.answer_question` |
| A "trigger an ingestion sync now" endpoint | **Does not exist.** Only an arq worker task and an hourly cron call `run_ingestion_job`. `test_ingestion_pipeline.py` calls that same real function directly — a disclosed substitution, not invented behavior. |
| An endpoint exposing rewritten query / per-candidate scores / reranked list / assembled context | **Does not exist.** `AskResponse` only returns `confidence`, `route_taken`, `answer`, `citations`, `investigation`. `test_retrieval_pipeline.py` calls the real internal functions directly to show this detail, clearly labeled as "internal introspection, not a REST call" everywhere it does so. |

## Files

- **`config.py`** — loads `.env`, builds a `ConnectorSpec` per connector (available/unavailable + the exact `credential_ref`/`config` shape each real connector's `authenticate()` expects).
- **`utils.py`** — shared event loop (see below), REST client, bootstrap-a-real-admin-identity helper, connector fetch loop, PASS/FAIL result recording/printing.
- **`test_connectors.py`** — Phase 1. Real `authenticate → fetch_batch → normalize → close` per connector.
- **`test_ingestion_pipeline.py`** — Phase 2. Registers a real connector config via REST, then calls the real `run_ingestion_job` directly, verifies documents/chunks/embeddings landed in Postgres and are retrievable.
- **`test_retrieval_pipeline.py`** — Phase 3. Real `POST /ask` calls, plus internal-introspection calls for the detailed pipeline trace.
- **`evaluate_answers.py`** — Phase 4. Independent LLM-as-judge (relevance/grounding/hallucination/citation accuracy/completeness), reusing EKIP's own `get_llm()` with a distinct judging prompt.
- **`questions.json`** — Phase 5 question bank with `expected_sources` hints.
- **`test_end_to_end_rag.py`** — Phase 5 automated runner over `questions.json`.
- **`run_rag_validation.py`** — Phase 6 master orchestrator + final report.

## The `asyncio.run()` pitfall, fixed from the start

`app.database.session.engine` is a module-level singleton whose pooled
`asyncpg` connections are bound to whichever event loop was running when
they opened. Calling `asyncio.run()` (which creates AND closes a new loop
every time) more than once in one process eventually reuses/closes a
connection tied to an already-closed loop, raising `RuntimeError: Event
loop is closed`. This was a real bug found and fixed in a sibling harness
(`scripts/realworld_onboarding/`); `utils.py`'s `run_async()` shares one
persistent event loop for the whole process from the start here instead.

## Troubleshooting

- **`ConnectionRefused`**: the API server isn't running at `EKIP_BASE_URL`.
- **A connector reports SKIPPED**: expected if you left that connector's
  credentials blank in `.env` — not a failure.
- **Teams/SharePoint 401**: the pasted Graph access token expired (they're
  short-lived, ~1 hour) — re-issue and re-paste.
- **`route_taken == "investigation"` in Phase 3/5**: confidence scored below
  `settings.confidence_threshold` (0.6 default) — usually means too little
  relevant data has been ingested yet for that question. Run
  `test_ingestion_pipeline.py` (or ingest more real data) first.
- **Judge (`evaluate_answers.py`) reports `parse_error`**: the judge LLM
  didn't return valid JSON that run — reported as FAIL, not silently
  ignored; a rare LLM formatting slip, re-run if it seems like an outlier.
- **`sentence-transformers`/cross-encoder model download fails**: needs
  outbound network access on first use (downloads from HuggingFace); not
  an EKIP or harness bug.

## Known real findings from building this harness (not yet empirically re-verified against a live run)

1. `AskResponse` (`POST /ask`) exposes no intermediate retrieval detail —
   rewritten query, per-candidate scores, reranked list, and assembled
   context are all internal-only today. A real product gap if API consumers
   ever need retrieval transparency/debuggability, not just a final answer.
2. There is no REST/MCP way to trigger an ingestion sync on demand — only
   the arq worker and an hourly cron call `run_ingestion_job`. An admin
   registering a new connector today has to wait up to an hour (or restart
   the worker) to see first results.
3. `expected_sources` in `questions.json` is necessarily a guess made before
   any real data was ingested against a specific customer's actual sources —
   treat `test_end_to_end_rag.py`'s source-overlap notes as advisory, not a
   hard correctness bar.
