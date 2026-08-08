"""Phase 3 -- Retrieval Pipeline Testing.

PURPOSE
    Send realistic enterprise questions through EKIP's real retrieval + RAG
    answer pipeline and print the complete trace: rewritten query, retrieved
    documents with scores, reranked documents, final assembled context,
    generated answer, and citations.

TWO DIFFERENT, BOTH-REAL WAYS THIS SCRIPT CALLS EKIP -- READ THIS FIRST
    1. REAL REST CALL: `POST /ask` (`app/api/routers/ask.py`), with a real
       bearer token. This is a genuine black-box API call, exactly as an
       external client would make it. Its real response shape
       (`AskResponse`) is only: `confidence`, `route_taken`, `answer`,
       `citations`, `investigation`. It does NOT expose the rewritten
       query, the retrieved candidate list, per-document similarity scores,
       the reranked list, or the assembled context block -- confirmed by
       reading `app/shared/schemas/agent_contracts.py::AskResponse`
       directly. No REST or MCP endpoint anywhere in this codebase exposes
       that intermediate detail. This is a real, disclosed API limitation,
       not something this script works around silently.

    2. INTERNAL INTROSPECTION (clearly labeled below, every time it prints):
       to show the intermediate detail the task asks for, this script
       ALSO calls the exact same real, unmodified functions the production
       graph itself calls, in the same order
       (`app/agents/retrieval/node.py::make_retrieval_agent_node` is the
       reference this mirrors): `rewrite_query` -> `retrieval_service.search`
       -> `rerank` -> `assemble_context` -> `generate_answer` ->
       `build_citations`. This is the same category of disclosed workaround
       as `utils.py`'s bootstrap/ingestion-trigger functions: a real gap
       (no API exposes this trace) is named and worked around by calling
       real project code directly, not invented or approximated.

    Both paths hit the same underlying pipeline and should produce
    consistent (though not necessarily byte-identical, since each is a
    separate LLM call) results -- this script prints both so you can see
    they agree.

REQUIRES
    A real `OPENAI_API_KEY` in the PROJECT's own `.env` (used by query
    rewriting, answer generation, and grounding's LLM-escalation path -- see
    `app/agents/llm.py`). Embeddings and reranking use local models (no API
    key needed) but do need outbound network access to download them from
    HuggingFace on first use.

    Ingested data to search over -- run `test_ingestion_pipeline.py` first,
    or this script's questions will legitimately return low-confidence/
    no-answer results (a correct, honest outcome for an empty knowledge
    base, not a bug).

RUN
    python tests/ingestion_retrieval/test_retrieval_pipeline.py

COMMON FAILURES
    - `route_taken == "investigation"` instead of `"answer"`: confidence
      scored below `settings.confidence_threshold` (default 0.6) -- means
      retrieval didn't find strong matches for that question, most often
      because too little/no relevant data has been ingested yet.
    - `answer == "NO_ANSWER"` equivalent / grounding empties the answer:
      `verify_grounding` stripped every generated sentence as unsupported by
      the retrieved context -- also a real, correct pipeline behavior for a
      question the ingested data can't actually answer.
    - 401 from `/ask`: bearer token expired or malformed -- re-run the
      bootstrap step (this script re-bootstraps every run, so this should
      be rare).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as harness_config  # noqa: E402
import utils  # noqa: E402

from app.agents.answer.citations import build_citations  # noqa: E402
from app.agents.answer.generation import generate_answer  # noqa: E402
from app.agents.llm import get_llm  # noqa: E402
from app.agents.retrieval.context_assembly import assemble_context  # noqa: E402
from app.agents.retrieval.reranking import rerank  # noqa: E402
from app.agents.retrieval.rewriting import rewrite_query  # noqa: E402
from app.database.session import session_scope  # noqa: E402
from app.retrieval import service as retrieval_service  # noqa: E402
from app.retrieval.schemas import SearchFilters  # noqa: E402
from app.shared.schemas import Identity  # noqa: E402

QUESTIONS = [
    "Where is the authentication configuration documented?",
    "What was the latest incident related to payments?",
    "Which engineer changed the database schema?",
    "Show me the deployment process.",
    "What are the required environment variables?",
]


async def _introspect(question: str, organization_id: uuid.UUID) -> dict:
    """INTERNAL INTROSPECTION -- see module docstring, point 2. Not a REST
    call; calls the same real functions the production graph calls."""
    llm = get_llm()
    actor = Identity.for_agent("rag_harness_query_test", organization_id)

    async with session_scope() as session:
        rewritten = await rewrite_query(
            session, query=question, incident_id=None, actor=actor, llm=llm, retry_count={}
        )
        filters = SearchFilters(organization_id=organization_id, permission_codes=frozenset())
        candidates = await retrieval_service.search(session, rewritten, filters, 40)
        reranked = await rerank(rewritten, candidates, top_k=20)
        assembled = assemble_context(reranked)
        answer = await generate_answer(llm, question, assembled) if assembled else "(no candidates retrieved)"
        citations = build_citations(answer, assembled) if assembled else []

    return {
        "rewritten_query": rewritten,
        "retrieved": [(c.title or c.source_url or str(c.chunk_id), c.score) for c in candidates[:5]],
        "reranked": [(c.title or c.source_url or str(c.chunk_id), c.score) for c in reranked[:5]],
        "context_chunk_count": len(assembled),
        "context_texts": [c.content for c in assembled],
        "answer": answer,
        "citations": [(c.document_id, c.excerpt[:80]) for c in citations],
    }


def _ask_via_rest(client: utils.ApiClient, token: str, question: str) -> dict | None:
    response = client.call("POST", "/ask", token=token, json_body={"query": question})
    if response.status_code != 200:
        print(f"  REST /ask FAILED: HTTP {response.status_code}: {response.text[:300]}")
        return None
    return response.json()


def main() -> bool:
    utils.reset_results()
    cfg = harness_config.load_config()
    identity = utils.bootstrap_admin_sync(
        org_name=cfg.org_name, org_slug=cfg.org_slug, email=cfg.admin_email, display_name=cfg.admin_display_name
    )
    organization_id = uuid.UUID(identity["organization_id"])
    client = utils.ApiClient(base_url=cfg.base_url, timeout_seconds=cfg.request_timeout_seconds)

    try:
        for question in QUESTIONS:
            print("\n" + "-" * 64)
            print(f'Question:\n"{question}"')

            print("\n[REAL REST CALL -- POST /ask]")
            rest_result = _ask_via_rest(client, identity["access_token"], question)
            if rest_result is not None:
                print(f"  route_taken: {rest_result['route_taken']}")
                print(f"  confidence: {rest_result['confidence']:.2f}")
                print(f"  answer: {rest_result.get('answer')}")
                print(f"  citations: {rest_result.get('citations')}")
            utils.record_result(f"POST /ask: {question[:40]}", rest_result is not None)

            print("\n[INTERNAL INTROSPECTION -- direct calls to the real pipeline functions, NOT a REST call]")
            try:
                trace = utils.run_async(_introspect(question, organization_id))
                print(f"  Query Rewrite: \"{trace['rewritten_query']}\"")
                print("  Retrieved Documents:")
                for i, (label, score) in enumerate(trace["retrieved"], start=1):
                    print(f"    {i}. {label}  score={score:.3f}")
                print("  Reranked Documents:")
                for i, (label, score) in enumerate(trace["reranked"], start=1):
                    print(f"    {i}. {label}  score={score:.3f}")
                print(f"  Final Context: {trace['context_chunk_count']} chunk(s) assembled")
                print(f"  Generated Answer:\n    {trace['answer']}")
                print("  Citations:")
                for doc_id, excerpt in trace["citations"]:
                    print(f"    [{doc_id}] {excerpt}")
                utils.record_result(f"introspection: {question[:40]}", True)
            except Exception as exc:  # noqa: BLE001
                print("--- FAILURE REPORT ---")
                print("Component: internal retrieval/answer pipeline introspection")
                print(f"Command: rewrite_query/search/rerank/assemble_context/generate_answer for {question!r}")
                print(f"Error: {type(exc).__name__}: {exc}")
                print("Expected: a full pipeline trace")
                print("Actual: an exception was raised")
                print("Possible Cause: missing/invalid OPENAI_API_KEY, or no ingested data to search")
                print("Suggested Fix: confirm OPENAI_API_KEY in the project's own .env; run test_ingestion_pipeline.py first")
                utils.record_result(f"introspection: {question[:40]}", False, detail=str(exc))
    finally:
        client.close()

    return utils.print_summary(title="EKIP RETRIEVAL PIPELINE TEST -- SUMMARY")


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
