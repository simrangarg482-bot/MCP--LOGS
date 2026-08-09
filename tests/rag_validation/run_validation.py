"""Real end-to-end RAG validation for EKIP.

WHAT THIS IS
    A standalone validation harness that runs realistic questions through
    EKIP's REAL, UNMODIFIED pipeline against the REAL data its connectors
    already ingested, and reports PASS/FAIL per question:

        question -> query rewrite -> hybrid retrieval -> cross-encoder
        reranking -> context assembly -> LLM answer -> citations

    Every stage above is the production implementation, called directly
    (`app.agents.retrieval.rewriting.rewrite_query`,
    `app.retrieval.service.search`, `app.agents.retrieval.reranking.rerank`,
    `app.agents.retrieval.context_assembly.assemble_context`,
    `app.agents.answer.generation.generate_answer`,
    `app.agents.answer.citations.build_citations`). Nothing is stubbed,
    faked, or re-implemented here -- this file only orchestrates and grades.

    Each question is ALSO run through the real production entry point
    `app.agents.service.answer_question`, which additionally applies the
    confidence gate and answer/investigation routing that a REST `POST /ask`
    would. That second path is what the negative controls are graded on:
    the direct component path always generates *something* once any context
    exists, so it cannot by itself tell you whether the system would have
    confidently served that answer to a user. The confidence gate can.

WHY IT IS A SCRIPT, NOT A pytest TEST
    It needs a live database with real ingested data, a real OPENAI_API_KEY,
    and takes minutes and real money per run. `pyproject.toml` sets
    `testpaths = ["tests"]`, so a `test_*.py` here would be collected into
    the ordinary unit-test suite and break it. This file is deliberately
    named so pytest will not collect it.

GRADING -- WHAT EACH CHECK ACTUALLY PROVES
    For `grounded` questions (answerable from the corpus), all four must
    hold to PASS:

      [RETRIEVAL]  At least one `must_retrieve_any` string appears in the
                   assembled context. Proves relevant evidence was actually
                   retrieved, rather than the answer coming from the LLM's
                   parametric memory.
      [ANSWER]     Every `expected_fact_groups` group is satisfied (each
                   group is a set of acceptable phrasings for ONE fact, so
                   paraphrase is allowed but the fact itself is required).
                   Proves the answer actually answers the question.
      [GROUNDING]  Deterministic: no expected fact appears in the answer
                   that is absent from the retrieved context. Plus an
                   independent LLM judge returning grounded=YES and
                   hallucination=NO.
      [CITATIONS]  At least one citation, and at least one citation whose
                   excerpt text is genuinely found in the assembled context
                   -- i.e. citations point at evidence really retrieved,
                   not at fabricated references.

    For `negative` questions (deliberately unanswerable), PASS means the
    system did NOT confidently answer: either the confidence gate routed to
    investigation, or it produced no answer, or it produced no citations, or
    confidence fell below `Settings.confidence_threshold`, or the answer
    explicitly declines. FAIL means it served a confident, cited answer to a
    question the corpus cannot support -- a confidently incorrect answer.

    The expected-source overlap check is reported but ADVISORY (it does not
    fail a question): a genuinely good, well-grounded answer can legitimately
    be cited from a source this dataset did not anticipate, since the same
    incidents are discussed in both GitHub and Slack.

INDEPENDENCE OF THE JUDGE
    EKIP's own `app.agents.answer.grounding.verify_grounding` already runs
    inside the production answer path. It is therefore NOT usable as this
    harness's grounding check -- that would be grading EKIP with its own
    answer key. The judge here is a separate prompt at temperature 0, and it
    is deliberately backed up by the deterministic fact/context checks above
    so a single flaky judge call cannot silently pass a bad answer.

RUN
    python tests/rag_validation/run_validation.py
    python tests/rag_validation/run_validation.py --only negative
    python tests/rag_validation/run_validation.py --org-slug test-org --limit 3

SIDE EFFECTS ON THE LIVE DATABASE
    `answer_question` records one `agent_executions` row per question (its
    normal production behavior). Nothing else is written. No application
    code and no existing test is modified by this harness.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# This harness's whole value is its readable per-question report. The app's
# engine is created with SQL echo on, which buries that report under hundreds
# of statement dumps -- quieted here, in this file only, rather than by
# touching `app.database.session`.
#
# `disabled`, not just `setLevel`: SQLAlchemy's `echo=True` re-raises its own
# logger's level to INFO when the engine is constructed, which happens lazily
# on first use -- i.e. after this module is imported -- so a level set here
# would simply be overwritten. `disabled` is not touched by echo.
def _silence(*logger_names: str) -> None:
    for name in logger_names:
        logger = logging.getLogger(name)
        logger.setLevel(logging.WARNING)
        logger.disabled = True


_silence("sqlalchemy.engine", "sqlalchemy.engine.Engine", "sqlalchemy.pool", "httpx", "openai")

from sqlalchemy import select  # noqa: E402

from app.agents import service as agents_service  # noqa: E402
from app.agents.answer.citations import build_citations  # noqa: E402
from app.agents.answer.generation import generate_answer  # noqa: E402
from app.agents.llm import get_llm  # noqa: E402
from app.agents.retrieval.context_assembly import assemble_context  # noqa: E402
from app.agents.retrieval.reranking import rerank  # noqa: E402
from app.agents.retrieval.rewriting import rewrite_query  # noqa: E402
from app.database.models.ingestion_models import Document  # noqa: E402
from app.database.models.tenancy_models import Organization  # noqa: E402
from app.database.session import session_scope, set_tenant_context  # noqa: E402
from app.retrieval import service as retrieval_service  # noqa: E402
from app.retrieval.schemas import SearchFilters  # noqa: E402
from app.shared.config.settings import get_settings  # noqa: E402
from app.shared.schemas import Identity  # noqa: E402

_DATASET_PATH = Path(__file__).resolve().parent / "rag_dataset.json"
_DEFAULT_ORG_SLUG = "test-org"
_SEARCH_LIMIT = 40
_RERANK_TOP_K = 20

_DECLINE_MARKERS = (
    "no information",
    "not contain",
    "does not contain",
    "no relevant",
    "cannot answer",
    "can't answer",
    "unable to answer",
    "insufficient",
    "not available in",
    "not found in",
    "no evidence",
    "not mentioned",
    "i don't know",
    "i do not know",
    "no data",
)

_JUDGE_PROMPT = """You are an impartial evaluator grading a retrieval-augmented answer. Judge STRICTLY \
against the retrieved context below. Do NOT use any outside knowledge of your own -- if the answer states \
something true in general but absent from the context, that is a hallucination.

Question:
{question}

Retrieved Context (everything the system had to answer from):
{context}

Generated Answer:
{answer}

Respond with ONLY a single JSON object, no other text, using exactly these keys:
{{
  "relevance": <integer 1-10, does the answer address the question asked>,
  "grounded": "YES" or "NO",
  "hallucination": "YES" or "NO",
  "reasoning": "<one or two sentence justification>"
}}"""


# --------------------------------------------------------------------------
# text matching helpers
# --------------------------------------------------------------------------


def _norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


def _contains(haystack: str, needle: str) -> bool:
    return _norm(needle) in _norm(haystack)


def _first_match(haystack: str, needles: list[str]) -> str | None:
    for needle in needles:
        if _contains(haystack, needle):
            return needle
    return None


def _strip_truncation_marker(excerpt: str) -> str:
    """`app.agents.answer.citations.build_citations` appends a literal '...'
    to an excerpt whenever the cited chunk is longer than its 300-character
    excerpt limit. That marker is the app's own truncation indicator -- it is
    not text that exists in the source content -- so it must be removed
    before checking whether the excerpt really appears in the retrieved
    context. Without this, every citation of a chunk longer than 300
    characters looks unverifiable, which is a bug in the check, not in the
    pipeline.
    """
    return excerpt[:-3] if excerpt.endswith("...") else excerpt


# --------------------------------------------------------------------------
# real pipeline drivers
# --------------------------------------------------------------------------


async def _resolve_org(slug: str) -> tuple[uuid.UUID, str, int]:
    """Resolve an organization by slug and count its live documents, so a
    run against an empty organization fails loudly here instead of silently
    reporting that retrieval 'found nothing' for every question.
    """
    async with session_scope() as session:
        row = (
            await session.execute(select(Organization).where(Organization.slug == slug))
        ).scalar_one_or_none()
        if row is None:
            raise SystemExit(f"No organization with slug {slug!r} exists in this database.")
        await set_tenant_context(session, row.id)
        doc_count = len(
            (
                await session.execute(
                    select(Document.id).where(
                        Document.organization_id == row.id, Document.deleted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        return row.id, row.name, doc_count


async def _trace_pipeline(question: str, organization_id: uuid.UUID) -> dict:
    """Drive the real component pipeline end to end and return every
    intermediate stage, so retrieval/reranking/context can be graded and
    printed -- not just the final answer.
    """
    llm = get_llm()
    actor = Identity.for_agent("rag_validation", organization_id)

    async with session_scope() as session:
        await set_tenant_context(session, organization_id)
        rewritten = await rewrite_query(
            session, query=question, incident_id=None, actor=actor, llm=llm, retry_count={}
        )
        filters = SearchFilters(
            organization_id=organization_id, permission_codes=frozenset()
        )
        candidates = await retrieval_service.search(session, rewritten, filters, _SEARCH_LIMIT)
        reranked = await rerank(rewritten, candidates, top_k=_RERANK_TOP_K)
        assembled = assemble_context(reranked)
        answer = await generate_answer(llm, question, assembled) if assembled else ""
        citations = build_citations(answer, assembled) if assembled else []

        doc_ids = {c.document_id for c in assembled} | {c.document_id for c in citations}
        sources = await _resolve_document_sources(session, list(doc_ids))

    return {
        "rewritten_query": rewritten,
        "candidates": candidates,
        "reranked": reranked,
        "assembled": assembled,
        "context_text": "\n\n".join(c.content for c in assembled),
        "answer": answer or "",
        "citations": citations,
        "doc_sources": sources,
    }


async def _resolve_document_sources(session, document_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[str, str]]:
    if not document_ids:
        return {}
    rows = (
        await session.execute(
            select(Document.id, Document.source, Document.title).where(Document.id.in_(document_ids))
        )
    ).all()
    return {row[0]: (row[1], row[2] or "") for row in rows}


async def _production_ask(question: str, organization_id: uuid.UUID):
    """The real production entry point, including the confidence gate and
    answer/investigation routing a REST `POST /ask` would go through.
    """
    actor = Identity.for_agent("rag_validation", organization_id)
    async with session_scope() as session:
        await set_tenant_context(session, organization_id)
        return await agents_service.answer_question(session, question, None, actor)


def _judge(question: str, answer: str, context_text: str) -> dict:
    llm = get_llm(temperature=0.0)
    prompt = _JUDGE_PROMPT.format(
        question=question,
        context=context_text or "(no context retrieved)",
        answer=answer or "(no answer produced)",
    )
    try:
        raw = str(llm.invoke(prompt).content)
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            raise ValueError(f"no JSON object in judge response: {raw[:200]!r}")
        parsed = json.loads(raw[start : end + 1])
        return {
            "relevance": int(parsed.get("relevance", 0)),
            "grounded": str(parsed.get("grounded", "NO")).upper() == "YES",
            "hallucination": str(parsed.get("hallucination", "YES")).upper() == "YES",
            "reasoning": str(parsed.get("reasoning", "")),
            "ok": True,
        }
    except Exception as exc:  # noqa: BLE001 - a judge failure is a reportable result
        return {
            "relevance": 0,
            "grounded": False,
            "hallucination": True,
            "reasoning": f"judge call/parse failed: {exc}",
            "ok": False,
        }


# --------------------------------------------------------------------------
# grading
# --------------------------------------------------------------------------


def _grade_grounded(entry: dict, trace: dict, ask, judge: dict) -> tuple[bool, list[str]]:
    checks: list[str] = []
    answer, context = trace["answer"], trace["context_text"]

    # [RETRIEVAL] relevant evidence actually retrieved
    hit = _first_match(context, entry["must_retrieve_any"])
    retrieval_ok = hit is not None
    checks.append(
        f"[RETRIEVAL] {'PASS' if retrieval_ok else 'FAIL'} - "
        + (f"context contains {hit!r}" if retrieval_ok else f"none of {entry['must_retrieve_any']} in context")
    )

    # [ANSWER] the answer actually answers the question
    missing_groups, matched_facts = [], []
    for group in entry["expected_fact_groups"]:
        match = _first_match(answer, group)
        if match is None:
            missing_groups.append(group)
        else:
            matched_facts.append(match)
    answer_ok = not missing_groups
    checks.append(
        f"[ANSWER]    {'PASS' if answer_ok else 'FAIL'} - "
        + (f"states expected fact(s) {matched_facts}" if answer_ok else f"missing required fact(s): {missing_groups}")
    )

    # [GROUNDING] deterministic (no answered fact absent from context) + judge
    ungrounded = [fact for fact in matched_facts if not _contains(context, fact)]
    grounding_ok = not ungrounded and judge["grounded"] and not judge["hallucination"]
    detail = []
    if ungrounded:
        detail.append(f"fact(s) in answer but NOT in retrieved context: {ungrounded}")
    detail.append(f"judge grounded={'YES' if judge['grounded'] else 'NO'}")
    detail.append(f"hallucination={'YES' if judge['hallucination'] else 'NO'}")
    checks.append(f"[GROUNDING] {'PASS' if grounding_ok else 'FAIL'} - " + "; ".join(detail))

    # [CITATIONS] present, and pointing at evidence really retrieved
    citations = trace["citations"]
    verifiable = [
        c for c in citations if c.excerpt and _contains(context, _strip_truncation_marker(c.excerpt))
    ]
    citations_ok = bool(citations) and bool(verifiable)
    checks.append(
        f"[CITATIONS] {'PASS' if citations_ok else 'FAIL'} - "
        f"{len(citations)} citation(s), {len(verifiable)} with excerpt verified present in retrieved context"
    )

    # advisory only -- see module docstring
    cited_sources = sorted({trace["doc_sources"].get(c.document_id, ("?", ""))[0] for c in citations})
    expected_sources = entry.get("expected_sources") or []
    if expected_sources and cited_sources:
        overlap = set(expected_sources) & set(cited_sources)
        checks.append(
            f"[sources]   {'ok' if overlap else 'note'} (advisory) - expected one of {expected_sources}, "
            f"cited from {cited_sources}"
        )

    return (retrieval_ok and answer_ok and grounding_ok and citations_ok), checks


def _grade_negative(entry: dict, trace: dict, ask, judge: dict) -> tuple[bool, list[str]]:
    checks: list[str] = []
    threshold = get_settings().confidence_threshold
    answer = (ask.answer or "").strip()

    declined_by_route = ask.route_taken != "answer"
    declined_by_empty = not answer
    declined_by_citations = not ask.citations
    declined_by_confidence = ask.confidence < threshold
    declined_in_text = any(marker in _norm(answer) for marker in _DECLINE_MARKERS)

    confidently_answered = not (
        declined_by_route
        or declined_by_empty
        or declined_by_citations
        or declined_by_confidence
        or declined_in_text
    )
    ok = not confidently_answered

    reasons = []
    if declined_by_route:
        reasons.append(f"routed to {ask.route_taken!r} instead of answering")
    if declined_by_empty:
        reasons.append("returned no answer")
    if declined_by_citations:
        reasons.append("returned no citations")
    if declined_by_confidence:
        reasons.append(f"confidence {ask.confidence:.3f} < threshold {threshold}")
    if declined_in_text:
        reasons.append("answer explicitly declines")

    checks.append(
        f"[REFUSAL]   {'PASS' if ok else 'FAIL'} - "
        + ("; ".join(reasons) if ok else
           f"served a confident cited answer (confidence={ask.confidence:.3f}, "
           f"{len(ask.citations)} citation(s)) to an unanswerable question")
    )
    checks.append(
        f"[judge]     advisory - hallucination={'YES' if judge['hallucination'] else 'NO'}; {judge['reasoning']}"
    )
    return ok, checks


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def _print_question_block(entry: dict, trace: dict, ask, judge: dict, passed: bool, checks: list[str]) -> None:
    print("\n" + "=" * 78, flush=True)
    print(f"[{entry['id']}]  ({entry['kind']})", flush=True)
    print(f"Q: {entry['question']}", flush=True)
    print("-" * 78, flush=True)

    print(f"Rewritten query : {trace['rewritten_query']!r}", flush=True)
    print(
        f"Retrieved       : {len(trace['candidates'])} candidate chunk(s) -> "
        f"{len(trace['reranked'])} reranked -> {len(trace['assembled'])} in final context",
        flush=True,
    )

    print("Retrieved sources (top 5 after reranking):", flush=True)
    if not trace["reranked"]:
        print("    (nothing retrieved)", flush=True)
    for i, chunk in enumerate(trace["reranked"][:5], start=1):
        source, doc_title = trace["doc_sources"].get(chunk.document_id, ("?", ""))
        label = chunk.title or doc_title or chunk.source_url or str(chunk.chunk_id)
        print(
            f"    {i}. [{source}/{chunk.collection}] {label[:64]!r}  score={chunk.score:.3f}",
            flush=True,
        )

    answer_text = trace["answer"].strip() or "(no answer produced)"
    print(f"\nGenerated answer (direct pipeline):\n    {answer_text}", flush=True)

    print(f"\nCitations ({len(trace['citations'])}):", flush=True)
    if not trace["citations"]:
        print("    (none)", flush=True)
    for citation in trace["citations"][:5]:
        source, doc_title = trace["doc_sources"].get(citation.document_id, ("?", ""))
        print(f"    - [{source}] {doc_title[:48]!r} :: {citation.excerpt[:90]!r}", flush=True)

    print(
        f"\nProduction /ask : route={ask.route_taken}  confidence={ask.confidence:.3f}  "
        f"citations={len(ask.citations)}",
        flush=True,
    )
    if ask.answer:
        print(f"    answer: {ask.answer.strip()[:300]}", flush=True)

    print("\nChecks:", flush=True)
    for check in checks:
        print(f"    {check}", flush=True)
    print(f"\nRESULT: {'PASS' if passed else 'FAIL'}", flush=True)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


async def _run(args) -> bool:
    dataset = json.loads(_DATASET_PATH.read_text(encoding="utf-8"))
    questions = dataset["questions"]
    if args.only:
        questions = [q for q in questions if q["kind"] == args.only]
    if args.limit:
        questions = questions[: args.limit]

    org_id, org_name, doc_count = await _resolve_org(args.org_slug)
    print("=" * 78, flush=True)
    print("EKIP -- REAL END-TO-END RAG VALIDATION", flush=True)
    print("=" * 78, flush=True)
    print(f"Organization : {org_name!r} (slug={args.org_slug}, id={org_id})", flush=True)
    print(f"Live documents in org : {doc_count}", flush=True)
    print(f"Questions to run      : {len(questions)}", flush=True)
    print(f"Confidence threshold  : {get_settings().confidence_threshold}", flush=True)
    if doc_count == 0:
        raise SystemExit(
            f"\nOrganization {args.org_slug!r} has 0 ingested documents -- every question would "
            "trivially 'fail' for lack of data. Point --org-slug at the organization your "
            "connectors actually ingested into."
        )

    results: list[tuple[str, str, bool]] = []
    for entry in questions:
        try:
            trace = await _trace_pipeline(entry["question"], org_id)
            ask = await _production_ask(entry["question"], org_id)
            judge = _judge(entry["question"], trace["answer"], trace["context_text"])
            if entry["kind"] == "negative":
                passed, checks = _grade_negative(entry, trace, ask, judge)
            else:
                passed, checks = _grade_grounded(entry, trace, ask, judge)
            _print_question_block(entry, trace, ask, judge, passed, checks)
        except Exception as exc:  # noqa: BLE001 - one bad question must not abort the run
            print("\n" + "=" * 78, flush=True)
            print(f"[{entry['id']}]  ({entry['kind']})", flush=True)
            print(f"Q: {entry['question']}", flush=True)
            print(f"\nRESULT: FAIL - harness error: {type(exc).__name__}: {exc}", flush=True)
            passed = False
        results.append((entry["id"], entry["kind"], passed))

    print("\n" + "=" * 78, flush=True)
    print("SUMMARY", flush=True)
    print("=" * 78, flush=True)
    for qid, kind, passed in results:
        print(f"  {'PASS' if passed else 'FAIL'}  [{kind:>8}]  {qid}", flush=True)
    total, passed_n = len(results), sum(1 for _, _, p in results if p)
    grounded = [(q, p) for q, k, p in results if k == "grounded"]
    negative = [(q, p) for q, k, p in results if k == "negative"]
    print(
        f"\n  grounded questions : {sum(1 for _, p in grounded if p)}/{len(grounded)} passed",
        flush=True,
    )
    print(
        f"  negative controls  : {sum(1 for _, p in negative if p)}/{len(negative)} passed",
        flush=True,
    )
    print(f"  TOTAL              : {passed_n}/{total} passed", flush=True)
    return passed_n == total


def main() -> int:
    parser = argparse.ArgumentParser(description="Real end-to-end RAG validation for EKIP.")
    parser.add_argument("--org-slug", default=_DEFAULT_ORG_SLUG, help="organization whose ingested data to query")
    parser.add_argument("--only", choices=["grounded", "negative"], help="run only one kind of question")
    parser.add_argument("--limit", type=int, help="run at most N questions")
    args = parser.parse_args()
    return 0 if asyncio.run(_run(args)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
