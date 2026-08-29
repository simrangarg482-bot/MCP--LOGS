"""`EvaluationRunner` -- wires a `Dataset` through the right adapters and
metrics for each case's `category`, producing one `EvaluationReport`.

    Dataset -> Runner -> adapters (retrieval / answer / investigation)
            -> metrics + assertions -> EvaluationResult -> EvaluationReport

**Mode selection** (`EvaluationMode`, `app.evaluation.schemas`) only changes
which adapters the caller passes in -- the runner itself has no branching on
mode beyond recording it on the report; see `build_deterministic_runner`
below for the Mode 1 (default, CI-safe) wiring, and each adapter module's
own docstring for what Mode 2 (`Real*Adapter`, real DB, no paid API) and
Mode 3 (`RealLLMJudge`, opt-in, real model calls) look like.

**The retrieval-vs-generation distinction** (this package's core design
requirement -- see `app.evaluation`'s own docstring) is decided per category:
- `retrieval` cases: any failure is definitionally a retrieval failure --
  that is the only thing this category tests.
- `grounding` cases: a failure is `retrieval` if expected concepts were
  never traceable to what was retrieved at all; it's `generation` if
  retrieval succeeded but the generated answer's citations don't resolve to
  / aren't supported by that retrieved evidence, or the answer text itself
  is missing a concept the evidence did contain.
- `answer` cases: `retrieval` if the case declares `relevant_document_ids`
  and they weren't found; `generation` otherwise (an assertion or
  confidence-range failure with the right evidence in hand).
- `investigation` cases: `retrieval` if required evidence was never
  gathered; `generation` if the evidence existed but hypotheses weren't
  produced/supported, or a hypothesis cites support that doesn't exist.
"""

from __future__ import annotations

import subprocess
import time
from datetime import UTC, datetime

from app.evaluation.adapters.generation import (
    AnswerAdapter,
    CritiqueAdapter,
    FixtureAnswerAdapter,
    FixtureCritiqueAdapter,
    FixtureInvestigationAdapter,
    InvestigationAdapter,
)
from app.evaluation.adapters.graph import FixtureGraphAdapter, GraphAdapter
from app.evaluation.adapters.llm import DEFAULT_LLM_JUDGE, LLMJudge
from app.evaluation.adapters.memory import FixtureMemoryAdapter, MemoryAdapter
from app.evaluation.adapters.proactive import FixtureProactiveAdapter, ProactiveAdapter
from app.evaluation.adapters.retrieval import FixtureRetrievalAdapter, RetrievalAdapter
from app.evaluation.adapters.semantic import DEFAULT_SIMILARITY_SCORER, SimilarityScorer
from app.evaluation.assertions.answer import evaluate_all
from app.evaluation.metrics import confidence as confidence_metrics
from app.evaluation.metrics import grounding as grounding_metrics
from app.evaluation.metrics import investigation as investigation_metrics
from app.evaluation.metrics import retrieval as retrieval_metrics
from app.evaluation.schemas import (
    Dataset,
    EvaluationCase,
    EvaluationMode,
    EvaluationReport,
    EvaluationResult,
    ExpectedInvestigation,
    FailureDetail,
    MetricResult,
)

#: Never one hardcoded K -- swept across every configured value, per this
#: package's "do not hardcode one value" requirement. Used only by the
#: `retrieval` category, which specifically wants to measure behavior across
#: a range of K.
DEFAULT_K_VALUES: tuple[int, ...] = (1, 3, 5, 10)

#: How many chunks form the evidence *context* for grounding/answer/
#: investigation cases -- deliberately smaller than `max(DEFAULT_K_VALUES)`
#: and configurable independently of it. A real Answer/Investigation Agent
#: never dumps its full retrieval candidate pool into context either
#: (`agents.retrieval.context_assembly` bounds it); reusing the largest
#: swept K here would mean a case only sharing one incidental word with an
#: unrelated document (e.g. both mentioning "authentication" in passing)
#: pulls that document into "evidence" and any of its content becomes fair
#: game for a forbidden-concept check -- a fixture-corpus artifact, not a
#: real grounding failure.
DEFAULT_CONTEXT_TOP_K = 3


def _git_commit() -> str | None:
    """Best-effort short commit hash for report provenance -- `None` (not an
    exception) outside a git checkout or without `git` on `PATH`."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


class EvaluationRunner:
    def __init__(
        self,
        *,
        mode: EvaluationMode,
        retrieval_adapter: RetrievalAdapter,
        answer_adapter: AnswerAdapter | None = None,
        investigation_adapter: InvestigationAdapter | None = None,
        critique_adapter: CritiqueAdapter | None = None,
        memory_adapter: MemoryAdapter | None = None,
        graph_adapter: GraphAdapter | None = None,
        proactive_adapter: ProactiveAdapter | None = None,
        similarity_scorer: SimilarityScorer = DEFAULT_SIMILARITY_SCORER,
        llm_judge: LLMJudge = DEFAULT_LLM_JUDGE,
        k_values: tuple[int, ...] = DEFAULT_K_VALUES,
        context_top_k: int = DEFAULT_CONTEXT_TOP_K,
        calibration_bucket_edges: tuple[float, ...] = confidence_metrics.DEFAULT_BUCKET_EDGES,
    ) -> None:
        self._mode = mode
        self._retrieval_adapter = retrieval_adapter
        self._answer_adapter = answer_adapter
        self._investigation_adapter = investigation_adapter
        self._critique_adapter = critique_adapter
        self._memory_adapter = memory_adapter
        self._graph_adapter = graph_adapter
        self._proactive_adapter = proactive_adapter
        self._similarity_scorer = similarity_scorer
        self._llm_judge = llm_judge
        self._k_values = k_values
        self._context_top_k = context_top_k
        self._calibration_bucket_edges = calibration_bucket_edges

    # ------------------------------------------------------------------
    # per-category case runners
    # ------------------------------------------------------------------

    async def _run_retrieval_case(self, case: EvaluationCase, start: float) -> EvaluationResult:
        top_k = max(self._k_values)
        chunks = await self._retrieval_adapter.search(case, top_k)
        retrieved_ids = [str(chunk.document_id) for chunk in chunks]
        relevant_ids = set(case.expected.relevant_document_ids)

        metrics: dict[str, MetricResult] = {}
        for k in self._k_values:
            metrics[f"recall_at_{k}"] = MetricResult(
                name=f"recall_at_{k}",
                value=retrieval_metrics.recall_at_k(retrieved_ids, relevant_ids, k),
            )
            metrics[f"precision_at_{k}"] = MetricResult(
                name=f"precision_at_{k}",
                value=retrieval_metrics.precision_at_k(retrieved_ids, relevant_ids, k),
            )
        metrics["mrr"] = MetricResult(
            name="mrr", value=retrieval_metrics.mean_reciprocal_rank(retrieved_ids, relevant_ids)
        )
        coverage = retrieval_metrics.relevant_document_coverage(retrieved_ids, relevant_ids)
        metrics["relevant_document_coverage"] = MetricResult(
            name="relevant_document_coverage", value=coverage
        )

        combined_text = " ".join(chunk.content for chunk in chunks)
        missing_concepts = grounding_metrics.check_required_concepts(
            combined_text, case.expected.required_concepts
        )
        forbidden_hits = grounding_metrics.check_forbidden_concepts(
            combined_text, case.expected.forbidden_concepts
        )
        coverage_ok = not relevant_ids or (coverage is not None and coverage >= 1.0)
        passed = coverage_ok and not missing_concepts and not forbidden_hits

        reasons = []
        if not coverage_ok:
            missing_docs = sorted(relevant_ids - set(retrieved_ids))
            reasons.append(f"missing relevant docs: {missing_docs}")
        if missing_concepts:
            reasons.append(f"missing required concepts: {missing_concepts}")
        if forbidden_hits:
            reasons.append(f"forbidden concepts present: {forbidden_hits}")

        failure = FailureDetail(
            stage="none" if passed else "retrieval",
            reason="; ".join(reasons) if reasons else "retrieval satisfied expectations",
        )
        return EvaluationResult(
            case_id=case.id,
            expected_outcome=case.expected_outcome,
            expected_failure_stage=case.expected_failure_stage,
            category="retrieval",
            mode=self._mode,
            passed=passed,
            failure=failure,
            retrieved_document_ids=retrieved_ids,
            metrics=metrics,
            duration_seconds=time.monotonic() - start,
            timestamp=datetime.now(UTC),
        )

    async def _run_grounding_case(self, case: EvaluationCase, start: float) -> EvaluationResult:
        top_k = self._context_top_k
        chunks = await self._retrieval_adapter.search(case, top_k)
        retrieved_ids = [str(chunk.document_id) for chunk in chunks]
        evidence_texts = [chunk.content for chunk in chunks]

        traceability = grounding_metrics.concepts_traceable_to_evidence(
            case.expected.required_concepts, evidence_texts
        )
        untraceable_concepts = [concept for concept, ok in traceability.items() if not ok]
        forbidden_in_evidence = grounding_metrics.check_forbidden_concepts(
            " ".join(evidence_texts), case.expected.forbidden_concepts
        )
        retrieval_failed = bool(untraceable_concepts) or bool(forbidden_in_evidence)

        traceability_scores = [1.0 if ok else 0.0 for ok in traceability.values()]
        metrics: dict[str, MetricResult] = {
            "concept_traceability_rate": MetricResult(
                name="concept_traceability_rate",
                value=retrieval_metrics.mean_of(traceability_scores) if traceability else None,
            )
        }

        confidence: float | None = None
        answer_text: str | None = None
        generation_reasons: list[str] = []

        should_check_citations = (
            not retrieval_failed
            and self._answer_adapter is not None
            and case.expected.citations is not None
        )
        if should_check_citations:
            answer_text, citations, confidence = await self._answer_adapter.generate_answer(
                case, chunks
            )
            chunk_ids = {chunk.chunk_id for chunk in chunks}
            chunk_contents = {chunk.chunk_id: chunk.content for chunk in chunks}
            citation_result = grounding_metrics.check_citations(
                citations, chunk_ids, chunk_contents, case.expected.citations
            )
            metrics["citation_count_satisfied"] = MetricResult(
                name="citation_count_satisfied",
                value=1.0 if citation_result.count_satisfied else 0.0,
            )
            metrics["citations_unresolved"] = MetricResult(
                name="citations_unresolved",
                value=float(len(citation_result.unresolved_citations)),
            )
            metrics["citations_unsupported"] = MetricResult(
                name="citations_unsupported",
                value=float(len(citation_result.unsupported_citations)),
            )
            if not citation_result.count_satisfied:
                minimum = case.expected.citations.minimum
                generation_reasons.append(f"expected >= {minimum} citations")
            if citation_result.unresolved_citations:
                unresolved = citation_result.unresolved_citations
                generation_reasons.append(f"unresolved citations: {unresolved}")
            if citation_result.unsupported_citations:
                unsupported_citations = citation_result.unsupported_citations
                generation_reasons.append(f"unsupported citations: {unsupported_citations}")

            answer_missing_concepts = grounding_metrics.check_required_concepts(
                answer_text, case.expected.required_concepts
            )
            if answer_missing_concepts:
                generation_reasons.append(
                    f"answer missing required concepts: {answer_missing_concepts}"
                )

        generation_failed = bool(generation_reasons)
        passed = not retrieval_failed and not generation_failed

        if passed:
            stage, reason = "none", "grounding satisfied"
        elif retrieval_failed:
            reasons = []
            if untraceable_concepts:
                reasons.append(
                    f"concepts not traceable to retrieved evidence: {untraceable_concepts}"
                )
            if forbidden_in_evidence:
                reasons.append(f"forbidden concepts present in evidence: {forbidden_in_evidence}")
            stage, reason = "retrieval", "; ".join(reasons)
        else:
            stage, reason = "generation", "; ".join(generation_reasons)

        return EvaluationResult(
            case_id=case.id,
            expected_outcome=case.expected_outcome,
            expected_failure_stage=case.expected_failure_stage,
            category="grounding",
            mode=self._mode,
            passed=passed,
            failure=FailureDetail(stage=stage, reason=reason),
            retrieved_document_ids=retrieved_ids,
            predicted_confidence=confidence,
            metrics=metrics,
            duration_seconds=time.monotonic() - start,
            timestamp=datetime.now(UTC),
            details={"answer": answer_text} if answer_text is not None else {},
        )

    async def _run_answer_case(self, case: EvaluationCase, start: float) -> EvaluationResult:
        if self._answer_adapter is None:
            raise RuntimeError("category 'answer' requires an AnswerAdapter to be configured")

        top_k = self._context_top_k
        chunks = await self._retrieval_adapter.search(case, top_k)
        retrieved_ids = [str(chunk.document_id) for chunk in chunks]

        relevant_ids = set(case.expected.relevant_document_ids)
        retrieval_ok = True
        if relevant_ids:
            coverage = retrieval_metrics.relevant_document_coverage(retrieved_ids, relevant_ids)
            retrieval_ok = coverage is not None and coverage >= 1.0

        answer_text, _citations, confidence = await self._answer_adapter.generate_answer(
            case, chunks
        )

        assertion_results = await evaluate_all(
            answer_text, case.expected.answer_assertions, similarity_scorer=self._similarity_scorer
        )
        failed_assertions = [(a, r) for a, r in assertion_results if not r.passed]
        confidence_ok = (
            case.expected.confidence.satisfied_by(confidence) if case.expected.confidence else True
        )

        passed = retrieval_ok and not failed_assertions and confidence_ok
        if passed:
            stage, reason = "none", "assertions and confidence satisfied"
        elif not retrieval_ok:
            stage = "retrieval"
            reason = (
                f"relevant evidence not retrieved: expected {sorted(relevant_ids)}, "
                f"got {retrieved_ids}"
            )
        else:
            reasons = [f"{a.type}={a.value!r}: {r.reason}" for a, r in failed_assertions]
            if not confidence_ok:
                reasons.append(f"confidence {confidence} outside expected range")
            stage, reason = "generation", "; ".join(reasons)

        assertion_pass_rate = None
        if assertion_results:
            assertion_pass_rate = (len(assertion_results) - len(failed_assertions)) / len(
                assertion_results
            )
        metrics = {
            "assertions_passed_rate": MetricResult(
                name="assertions_passed_rate", value=assertion_pass_rate
            )
        }
        return EvaluationResult(
            case_id=case.id,
            expected_outcome=case.expected_outcome,
            expected_failure_stage=case.expected_failure_stage,
            category="answer",
            mode=self._mode,
            passed=passed,
            failure=FailureDetail(stage=stage, reason=reason),
            retrieved_document_ids=retrieved_ids,
            predicted_confidence=confidence,
            metrics=metrics,
            duration_seconds=time.monotonic() - start,
            timestamp=datetime.now(UTC),
            details={"answer": answer_text},
        )

    async def _run_investigation_case(self, case: EvaluationCase, start: float) -> EvaluationResult:
        if self._investigation_adapter is None:
            raise RuntimeError(
                "category 'investigation' requires an InvestigationAdapter to be configured"
            )

        top_k = self._context_top_k
        chunks = await self._retrieval_adapter.search(case, top_k)
        retrieved_ids = [str(chunk.document_id) for chunk in chunks]

        expected_investigation = case.expected.investigation or ExpectedInvestigation()
        evidence, hypotheses = await self._investigation_adapter.investigate(case, chunks)

        coverage = investigation_metrics.evidence_coverage(
            evidence, expected_investigation.required_evidence_ids
        )
        match_results = investigation_metrics.match_expected_hypotheses(
            hypotheses, expected_investigation.hypotheses
        )
        unsupported = investigation_metrics.find_unsupported_hypotheses(hypotheses, evidence)

        evidence_ok = coverage is None or coverage >= 1.0
        hypotheses_ok = all(match.passed for match in match_results)

        # Priority 7: only checked when the case actually populates
        # `expected.investigation.critique` -- every pre-Priority-7 case
        # leaves it `None` and this block is skipped entirely, so their
        # behavior is unchanged.
        critique_expectation = expected_investigation.critique
        critique_ok = True
        review_status: str | None = None
        critique_verdict: str | None = None
        revision_count = 0
        if critique_expectation is not None:
            if self._critique_adapter is None:
                raise RuntimeError(
                    "case declares expected.investigation.critique but no CritiqueAdapter "
                    "is configured"
                )
            review_status, critique_verdict, revision_count = await self._critique_adapter.critique(
                case
            )
            if critique_expectation.expect_review_failed:
                critique_ok = review_status == "review_failed"
            elif critique_expectation.expected_verdict is not None:
                critique_ok = (
                    review_status == "reviewed"
                    and critique_verdict == critique_expectation.expected_verdict
                )

        passed = evidence_ok and hypotheses_ok and not unsupported and critique_ok

        if passed:
            stage, reason = "none", "evidence and hypotheses satisfied"
        elif not evidence_ok:
            stage = "retrieval"
            reason = f"required evidence not gathered (coverage={coverage})"
        else:
            reasons = []
            never_matched = [m.expected.concept for m in match_results if not m.matched]
            under_supported = [
                m.expected.concept for m in match_results if m.matched and not m.support_satisfied
            ]
            if never_matched:
                reasons.append(
                    f"expected concepts not mentioned by any produced hypothesis: {never_matched}"
                )
            if under_supported:
                reasons.append(
                    "hypotheses matched but under-supported (below minimum_support): "
                    f"{under_supported}"
                )
            if unsupported:
                reasons.append(
                    f"{len(unsupported)} hypothesis(es) cite no real supporting evidence"
                )
            if not critique_ok:
                reasons.append(
                    f"critique outcome (review_status={review_status!r}, "
                    f"critique_verdict={critique_verdict!r}) did not match expectation"
                )
            stage, reason = "generation", "; ".join(reasons)

        hypothesis_match_rate = None
        if match_results:
            hypothesis_match_rate = sum(m.passed for m in match_results) / len(match_results)
        metrics = {
            "evidence_coverage": MetricResult(name="evidence_coverage", value=coverage),
            "hypothesis_match_rate": MetricResult(
                name="hypothesis_match_rate", value=hypothesis_match_rate
            ),
        }
        if critique_expectation is not None:
            metrics["critique_revision_count"] = MetricResult(
                name="critique_revision_count", value=float(revision_count)
            )
        return EvaluationResult(
            case_id=case.id,
            expected_outcome=case.expected_outcome,
            expected_failure_stage=case.expected_failure_stage,
            category="investigation",
            mode=self._mode,
            passed=passed,
            failure=FailureDetail(stage=stage, reason=reason),
            retrieved_document_ids=retrieved_ids,
            metrics=metrics,
            duration_seconds=time.monotonic() - start,
            timestamp=datetime.now(UTC),
        )

    async def _run_memory_case(self, case: EvaluationCase, start: float) -> EvaluationResult:
        """Grade permission-aware memory recall.

        Failure stage is always `"retrieval"`: this category measures which
        memories were *selected*, which is entirely a recall/authorization
        question -- no generation happens. A `forbidden_labels` violation and
        a missing `expected_labels` entry are both retrieval-stage failures,
        though they mean very different things, so the reason distinguishes
        them explicitly.
        """
        if self._memory_adapter is None:
            raise RuntimeError("category 'memory' requires a MemoryAdapter to be configured")

        expectation = case.expected.memory
        if expectation is None:
            raise RuntimeError(
                f"case {case.id!r} has category='memory' but no expected.memory block"
            )

        recalled = await self._memory_adapter.recall(case, self._context_top_k)
        recalled_set = set(recalled)

        missing = [label for label in expectation.expected_labels if label not in recalled_set]
        leaked = [label for label in expectation.forbidden_labels if label in recalled_set]

        passed = not missing and not leaked
        reasons = []
        if missing:
            reasons.append(f"expected memories not recalled: {missing}")
        if leaked:
            # Worth its own wording: this is the leak case, not a miss.
            reasons.append(f"memories that must NOT be recalled were returned: {leaked}")

        expected_count = len(expectation.expected_labels)
        metrics = {
            "memory_recall_rate": MetricResult(
                name="memory_recall_rate",
                value=((expected_count - len(missing)) / expected_count)
                if expected_count
                else None,
            ),
            "memory_leaked_count": MetricResult(
                name="memory_leaked_count", value=float(len(leaked))
            ),
        }
        return EvaluationResult(
            case_id=case.id,
            expected_outcome=case.expected_outcome,
            expected_failure_stage=case.expected_failure_stage,
            category="memory",
            mode=self._mode,
            passed=passed,
            failure=FailureDetail(
                stage="none" if passed else "retrieval",
                reason="; ".join(reasons) if reasons else "memory recall matched expectations",
            ),
            metrics=metrics,
            duration_seconds=time.monotonic() - start,
            timestamp=datetime.now(UTC),
            details={"recalled": recalled},
        )

    async def _run_graph_case(self, case: EvaluationCase, start: float) -> EvaluationResult:
        """Grade permission-aware, bounded graph traversal.

        Failure stage is always `"retrieval"`: like the memory category,
        this tests which entities were *reached*, which is entirely a
        traversal/authorization question -- no generation happens. A leaked
        forbidden entity and a missing expected one are both retrieval-stage
        failures, though they mean very different things, so the reason
        distinguishes them explicitly (matching `_run_memory_case`).
        """
        if self._graph_adapter is None:
            raise RuntimeError("category 'graph' requires a GraphAdapter to be configured")

        expectation = case.expected.graph
        if expectation is None:
            raise RuntimeError(f"case {case.id!r} has category='graph' but no expected.graph block")

        reached = await self._graph_adapter.traverse(case, expectation.depth)
        reached_set = set(reached)

        missing = [label for label in expectation.expected_labels if label not in reached_set]
        leaked = [label for label in expectation.forbidden_labels if label in reached_set]

        passed = not missing and not leaked
        reasons = []
        if missing:
            reasons.append(f"expected entities not reached: {missing}")
        if leaked:
            # Worth its own wording: this is the leak case, not a miss.
            reasons.append(f"entities that must NOT be reachable were returned: {leaked}")

        expected_count = len(expectation.expected_labels)
        metrics = {
            "graph_reach_rate": MetricResult(
                name="graph_reach_rate",
                value=((expected_count - len(missing)) / expected_count)
                if expected_count
                else None,
            ),
            "graph_leaked_count": MetricResult(name="graph_leaked_count", value=float(len(leaked))),
        }
        return EvaluationResult(
            case_id=case.id,
            expected_outcome=case.expected_outcome,
            expected_failure_stage=case.expected_failure_stage,
            category="graph",
            mode=self._mode,
            passed=passed,
            failure=FailureDetail(
                stage="none" if passed else "retrieval",
                reason="; ".join(reasons) if reasons else "graph traversal matched expectations",
            ),
            metrics=metrics,
            duration_seconds=time.monotonic() - start,
            timestamp=datetime.now(UTC),
            details={"reached": reached},
        )

    async def _run_proactive_case(self, case: EvaluationCase, start: float) -> EvaluationResult:
        """Grade permission-aware, mixed-visibility proactive-finding
        resolution.

        Failure stage is always `"retrieval"`: like the memory/graph
        categories, this tests which findings were *selected*, an
        authorization/reachability question, no generation happens.
        """
        if self._proactive_adapter is None:
            raise RuntimeError("category 'proactive' requires a ProactiveAdapter to be configured")

        expectation = case.expected.proactive
        if expectation is None:
            raise RuntimeError(
                f"case {case.id!r} has category='proactive' but no expected.proactive block"
            )

        visible = await self._proactive_adapter.list_findings(case)
        visible_set = set(visible)

        missing = [label for label in expectation.expected_labels if label not in visible_set]
        leaked = [label for label in expectation.forbidden_labels if label in visible_set]

        passed = not missing and not leaked
        reasons = []
        if missing:
            reasons.append(f"expected findings not visible: {missing}")
        if leaked:
            # Worth its own wording: this is the leak case, not a miss.
            reasons.append(f"findings that must NOT be visible were returned: {leaked}")

        expected_count = len(expectation.expected_labels)
        metrics = {
            "proactive_visibility_rate": MetricResult(
                name="proactive_visibility_rate",
                value=((expected_count - len(missing)) / expected_count)
                if expected_count
                else None,
            ),
            "proactive_leaked_count": MetricResult(
                name="proactive_leaked_count", value=float(len(leaked))
            ),
        }
        return EvaluationResult(
            case_id=case.id,
            expected_outcome=case.expected_outcome,
            expected_failure_stage=case.expected_failure_stage,
            category="proactive",
            mode=self._mode,
            passed=passed,
            failure=FailureDetail(
                stage="none" if passed else "retrieval",
                reason="; ".join(reasons) if reasons else "proactive findings matched expectations",
            ),
            metrics=metrics,
            duration_seconds=time.monotonic() - start,
            timestamp=datetime.now(UTC),
            details={"visible": visible},
        )

    async def run_case(self, case: EvaluationCase) -> EvaluationResult:
        start = time.monotonic()
        if case.category == "retrieval":
            return await self._run_retrieval_case(case, start)
        if case.category == "grounding":
            return await self._run_grounding_case(case, start)
        if case.category == "answer":
            return await self._run_answer_case(case, start)
        if case.category == "investigation":
            return await self._run_investigation_case(case, start)
        if case.category == "memory":
            return await self._run_memory_case(case, start)
        if case.category == "graph":
            return await self._run_graph_case(case, start)
        if case.category == "proactive":
            return await self._run_proactive_case(case, start)
        raise ValueError(f"unknown evaluation category: {case.category!r}")  # pragma: no cover

    # ------------------------------------------------------------------
    # dataset / report assembly
    # ------------------------------------------------------------------

    def _aggregate_metrics(self, results: list[EvaluationResult]) -> dict[str, MetricResult]:
        names: set[str] = set()
        for result in results:
            names.update(result.metrics.keys())
        aggregate: dict[str, MetricResult] = {}
        for name in sorted(names):
            values = [result.metrics[name].value for result in results if name in result.metrics]
            aggregate[name] = MetricResult(
                name=name, value=retrieval_metrics.mean_of(values), details={"n": len(values)}
            )
        return aggregate

    def _calibration(self, results: list[EvaluationResult]):
        pairs = [
            (result.predicted_confidence, result.passed)
            for result in results
            if result.predicted_confidence is not None
        ]
        if not pairs:
            return None
        return confidence_metrics.compute_calibration(pairs, self._calibration_bucket_edges)

    async def run_dataset(self, dataset: Dataset) -> EvaluationReport:
        results = [await self.run_case(case) for case in dataset.cases]
        provider, model_name = self._llm_judge.model_info()
        return EvaluationReport(
            dataset_name=dataset.metadata.dataset_name,
            dataset_version=dataset.metadata.version,
            mode=self._mode,
            generated_at=datetime.now(UTC),
            git_commit=_git_commit(),
            model_provider=provider,
            model_name=model_name,
            results=results,
            aggregate_metrics=self._aggregate_metrics(results),
            calibration=self._calibration(results),
        )

    async def run_datasets(
        self, datasets: list[Dataset], *, combined_name: str = "combined"
    ) -> EvaluationReport:
        """Run several datasets and merge them into one report -- the CLI's
        default "run everything" mode. `dataset_version` on the combined
        report is a slash-joined list of each source dataset's own version,
        since there is no single meaningful version for a merge of several.
        """
        all_results: list[EvaluationResult] = []
        for dataset in datasets:
            report = await self.run_dataset(dataset)
            all_results.extend(report.results)
        provider, model_name = self._llm_judge.model_info()
        return EvaluationReport(
            dataset_name=combined_name,
            dataset_version="/".join(d.metadata.version for d in datasets),
            mode=self._mode,
            generated_at=datetime.now(UTC),
            git_commit=_git_commit(),
            model_provider=provider,
            model_name=model_name,
            results=all_results,
            aggregate_metrics=self._aggregate_metrics(all_results),
            calibration=self._calibration(all_results),
        )


def build_deterministic_runner(
    corpus: list,
    canned_answers: dict,
    canned_investigations: dict,
    memory_corpus: list | None = None,
    graph_entities: dict | None = None,
    graph_edges: list | None = None,
    proactive_findings: list | None = None,
    proactive_evidence: list | None = None,
    canned_critiques: dict | None = None,
) -> EvaluationRunner:
    """Mode 1 convenience constructor: wires the fixture adapters together
    with no external dependency. `corpus`/`canned_answers`/
    `canned_investigations` normally come from `app.evaluation.fixtures`
    (see that package) -- accepted as parameters here, not imported
    directly, so unit tests can pass in their own minimal fixtures without
    needing this package's full shipped dataset.

    `graph_entities`/`graph_edges` and `proactive_findings`/
    `proactive_evidence` follow the same "None means don't wire this
    adapter" convention as `memory_corpus` -- but unlike it, `None` here
    still builds their fixture adapters, defaulting to each module's
    shipped fixture data, since (unlike memory) there is exactly one
    fixture corpus shipped for each and every caller of this constructor
    wants it unless it explicitly overrides it.

    `canned_critiques` (Priority 7) follows `memory_corpus`'s "`None` means
    don't wire this adapter" convention instead -- most investigation cases
    don't exercise critique at all (`ExpectedInvestigation.critique` stays
    `None` for them), so forcing a `FixtureCritiqueAdapter` on every caller
    would mean an unrelated case could raise `KeyError` for a fixture it
    never needed.
    """
    return EvaluationRunner(
        mode="deterministic",
        retrieval_adapter=FixtureRetrievalAdapter(corpus),
        answer_adapter=FixtureAnswerAdapter(canned_answers),
        investigation_adapter=FixtureInvestigationAdapter(canned_investigations),
        critique_adapter=FixtureCritiqueAdapter(canned_critiques)
        if canned_critiques is not None
        else None,
        memory_adapter=FixtureMemoryAdapter(memory_corpus) if memory_corpus is not None else None,
        graph_adapter=FixtureGraphAdapter(graph_entities, graph_edges),
        proactive_adapter=FixtureProactiveAdapter(proactive_findings, proactive_evidence),
    )
