"""Pydantic contracts for `app.evaluation.semantic` -- the live, real-model
benchmark layer (Tier 3). Deliberately separate from `app.evaluation.
schemas` (Tier 1's deterministic `EvaluationCase`/`EvaluationResult`):
`expected_outcome`/`matched_expectation`/`regression_kind` mean "did this
case defy a deterministic prediction" and must keep meaning exactly that.
A semantic quality score (0.72 faithfulness, say) is a different kind of
claim -- there is no ground-truth "predicted" value to defy, only a
measurement to report and, once enough of them exist, calibrate a threshold
against. Reusing the Tier 1 vocabulary for this would blur a real
regression gate with an inherently noisy live-model measurement.

PROVENANCE IS NOT OPTIONAL
    Every benchmark case declares where it came from
    (`BenchmarkCaseProvenance`) -- this priority's own spec is explicit
    that a small, honestly-labelled corpus is acceptable but a fabricated
    "realistic" one is not. A report built from this package always shows
    provenance, so nothing here can be quietly mistaken for calibration
    against real production traffic.

CALIBRATION STATUS IS A FIRST-CLASS TYPE, NOT A COMMENT
    `CalibrationStatus` is the vocabulary this whole package uses to avoid
    the single failure mode this priority exists to prevent: a constant
    added, casually described as "seems reasonable," and never revisited.
    A value is `provisional` until real measurement (with a stated sample
    size) supports `calibrated`; `insufficient_data` is a legitimate,
    honestly-reported outcome, not a bug in the harness.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Where a benchmark case's content came from. Every case declares one --
#: see module docstring. `"repository_derived"` means built from real
#: content already ingested into this repository's own dev/eval database
#: (e.g. `tests/rag_validation`'s corpus); `"sanitized_real"` means a real
#: organizational example with identifying detail removed; neither is
#: claimed for anything in this initial pass -- see
#: `docs/SEMANTIC_BENCHMARK.md`.
BenchmarkCaseProvenance = Literal[
    "synthetic_controlled", "repository_derived", "sanitized_real", "manually_curated"
]

#: What kind of question one benchmark case answers. Kept small: only
#: categories this package actually measures.
BenchmarkCategory = Literal["answer_quality", "investigation_ab"]

#: The vocabulary a threshold's calibration status is reported in -- see
#: module docstring. Extended (Priority 11) with three human-ground-truth-
#: specific reasons a threshold can still not be `calibrated` even once a
#: sweep produces a clean number: `insufficient_agreement` (reviewers
#: themselves don't agree enough to trust the labels),
#: `insufficient_class_coverage` (human labels exist but don't span the
#: outcome classes that matter), `not_applicable` (this setting isn't the
#: kind of thing human-labeled answer-quality ground truth could ever
#: speak to -- a structural bound, not a data gap). Deliberately separate
#: values, not folded into `insufficient_data`: each names a DIFFERENT
#: reason a claim isn't earned yet, and collapsing them would hide which
#: one applies (section 16's "do not overload one status to hide different
#: reasons").
CalibrationStatus = Literal[
    "calibrated",
    "provisional",
    "insufficient_data",
    "intentionally_fixed_domain_rule",
    "insufficient_agreement",
    "insufficient_class_coverage",
    "not_applicable",
    "scope_excluded",
]

#: Distinguishes "the benchmark could not run" from "the benchmark ran and
#: found a quality problem" -- section 6's central requirement. A verdict
#: of `benchmark_execution_failed` means infrastructure (missing
#: credentials, an unreachable model, malformed evaluator output that
#: exhausted retries); every other verdict means the benchmark executed and
#: is reporting what it measured.
BenchmarkVerdict = Literal[
    "baseline_established",
    "quality_target_met",
    "regression_detected",
    "insufficient_data",
    "benchmark_execution_failed",
]


# --------------------------------------------------------------------------
# answer-quality rubric -- refusal-aware (Priority 9)
# --------------------------------------------------------------------------

#: What the evidence *should* justify, declared by the case author -- NEVER
#: inferred from what a model happened to output (Priority 9 section 5: that
#: would let the model grade its own behavior). Named after, and directly
#: informed by, the production `SufficiencyVerdict`
#: (`app.agents.answer.sufficiency`) this benchmark already has: "answer"
#: corresponds to that check's "sufficient", "qualified_answer" to
#: "partial", "no_answer" to "insufficient". Kept as a distinct type rather
#: than importing `SufficiencyVerdict` directly -- this is the benchmark's
#: own ground-truth *declaration* about a case, not the live pre-generation
#: gate's runtime output, and the two must be free to diverge (e.g. a case
#: can declare its expected mode before any model ever runs against it).
#: `"unlabeled"` is a first-class value, not a missing one: a case whose
#: expected mode cannot be reliably derived (see `fixtures.py`'s
#: repository-derived loader) is declared `"unlabeled"` rather than guessed,
#: and is excluded from outcome-correctness metrics -- see
#: `outcome.classify_outcome_correctness`.
ExpectedAnswerMode = Literal["answer", "qualified_answer", "no_answer", "unlabeled"]

#: What a generated answer actually did, as determined by evaluation (see
#: `outcome.py`): `"no_answer"` is detected deterministically (the model's
#: own `NO_ANSWER` marker, or the Answer Agent node's fixed insufficient-
#: grounding sentinel -- both reused, never re-implemented, from
#: `app.agents.answer.generation`/`app.agents.answer.node`); the
#: `"substantive_answer"` vs `"qualified_answer"` distinction is inherently
#: semantic (did the text meaningfully hedge/flag uncertainty, or state
#: things directly) and is classified by the same judge call that scores
#: the substantive rubric dimensions -- see `answer_quality.py`.
ObservedAnswerMode = Literal["substantive_answer", "qualified_answer", "no_answer"]

#: The deterministic comparison of `ExpectedAnswerMode` vs `ObservedAnswerMode`
#: (`outcome.classify_outcome_correctness`) -- pure lookup, no LLM call.
#: `"correct"` covers three genuinely different good outcomes (a right
#: substantive answer, a right qualification, a right refusal) on purpose:
#: this priority's whole point is that all three deserve equal credit, not
#: that they collapse into one meaning. `"critical_failure"` is reserved for
#: the worst case this benchmark exists to catch: a substantive or qualified
#: answer produced where the evidence had no real bearing on the question at
#: all (`expected_answer_mode == "no_answer"`) -- a hallucination, not a
#: mere miscalibration.
AnswerOutcomeCorrectness = Literal[
    "correct", "partially_correct", "overconfident", "incorrect_refusal", "critical_failure"
]


class AnswerQualityCase(BaseModel):
    """One case for the structured answer-quality evaluator.

    `reference_answer` is optional -- not every real question has a known-
    good reference; when absent, the evaluator judges the generated answer
    against `evidence` alone (faithfulness/correctness relative to what was
    actually retrievable), not against a template it must match.

    `fixed_answer`, when set, is judged directly instead of calling
    `agents.answer.generation.generate_answer` -- the mechanism this
    priority's contrast fixtures (section 6: same question/evidence, a
    deliberately different answer text, a deliberately different expected
    evaluation) use to test the EVALUATOR's discrimination without needing
    to coax a specific failure mode out of a live model, which cannot be
    reliably forced. `None` (the default) preserves every existing case's
    behavior byte-for-byte: still generated live.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    provenance: BenchmarkCaseProvenance
    question: str
    #: Evidence IDs/text the case supplies directly, OR left empty to mean
    #: "run real retrieval for this query" -- see `runner.py` for exactly
    #: which mode a given case exercises.
    evidence_texts: list[str] = Field(default_factory=list)
    reference_answer: str | None = None
    #: See module-level `ExpectedAnswerMode` docstring. Defaults to
    #: `"unlabeled"`, never guessed.
    expected_answer_mode: ExpectedAnswerMode = "unlabeled"
    fixed_answer: str | None = None
    tags: list[str] = Field(default_factory=list)


class AnswerQualityDimension(BaseModel):
    """One rubric dimension's score + the evaluator's stated reason --
    never a bare number. `score` is 0.0-1.0; there is no dimension whose
    definition changes per case, so scores across cases are comparable."""

    model_config = ConfigDict(frozen=True)

    score: float = Field(ge=0.0, le=1.0)
    reason: str


class SubstantiveAnswerJudgement(BaseModel):
    """The rubric applied when the evaluated text is NOT a refusal (see
    `outcome.is_refusal_text`) -- covers both a fully substantive answer and
    an answer that hedges/qualifies. `observed_mode` is this same call's own
    classification of which of those two it is (Priority 9 section 7: one
    mode-aware judge call, not a separate classification call)."""

    model_config = ConfigDict(frozen=True)

    observed_mode: Literal["substantive_answer", "qualified_answer"]
    correctness: AnswerQualityDimension
    relevance: AnswerQualityDimension
    usefulness: AnswerQualityDimension
    faithfulness: AnswerQualityDimension

    @property
    def mean_score(self) -> float:
        return (
            self.correctness.score
            + self.relevance.score
            + self.usefulness.score
            + self.faithfulness.score
        ) / 4.0


class RefusalJudgement(BaseModel):
    """The rubric applied when `outcome.is_refusal_text` deterministically
    detects a refusal -- Priority 9 section 4's four abstention-specific
    dimensions, deliberately NOT the substantive rubric's four (a refusal
    has no "citation faithfulness" to score against; scoring it as if it
    did is exactly the bug this priority exists to fix). A refusal does not
    receive full credit merely for existing -- `abstention_correctness`
    distinguishes a correct abstention from a lazy one (declining when the
    evidence actually did answer the question)."""

    model_config = ConfigDict(frozen=True)

    #: Was declining actually justified by the evidence? Low when the
    #: evidence clearly supported an answer -- the "lazy refusal" case
    #: (section 4's own worked example).
    abstention_correctness: AnswerQualityDimension
    #: Did the refusal's own explanation avoid inventing facts not present
    #: in the evidence while explaining the limitation?
    unsupported_claim_avoidance: AnswerQualityDimension
    #: Is the explanation clear and honest about what is/isn't known,
    #: without misleadingly implying evidence exists that doesn't?
    explanation_quality: AnswerQualityDimension
    #: Where appropriate, did it suggest a useful way to obtain the missing
    #: information, without fabricating an answer to avoid saying "I don't
    #: know"?
    appropriate_next_step: AnswerQualityDimension

    @property
    def mean_score(self) -> float:
        return (
            self.abstention_correctness.score
            + self.unsupported_claim_avoidance.score
            + self.explanation_quality.score
            + self.appropriate_next_step.score
        ) / 4.0


class AnswerJudgement(BaseModel):
    """One evaluator call's full output: which mode was observed, plus
    exactly one of the two mode-specific judgements (never both, never
    neither) -- `outcome_correctness` is deliberately NOT stored here, since
    it is a pure function of `observed_mode` and the case's own
    `expected_answer_mode` (`outcome.classify_outcome_correctness`), computed
    by the caller rather than duplicated as evaluator output."""

    model_config = ConfigDict(frozen=True)

    observed_answer_mode: ObservedAnswerMode
    substantive: SubstantiveAnswerJudgement | None = None
    refusal: RefusalJudgement | None = None

    @property
    def mean_score(self) -> float:
        judgement = self.substantive or self.refusal
        assert judgement is not None  # guaranteed by judge_answer_quality's construction
        return judgement.mean_score


class AnswerQualityResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    question: str
    generated_answer: str
    expected_answer_mode: ExpectedAnswerMode
    judgement: AnswerJudgement | None
    #: `outcome.classify_outcome_correctness(expected_answer_mode,
    #: judgement.observed_answer_mode)` -- `None` when `expected_answer_mode
    #: == "unlabeled"` (excluded from outcome-correctness metrics, per
    #: section 5) or when `judgement` is `None` (evaluator error).
    outcome_correctness: AnswerOutcomeCorrectness | None = None
    #: Set when the evaluator call itself failed (malformed output,
    #: exhausted retries, model error) -- `judgement` is `None` in that
    #: case. Never silently treated as a low score; excluded from
    #: aggregation, counted separately (see `SemanticBenchmarkReport`).
    error: str | None = None
    latency_seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


# --------------------------------------------------------------------------
# investigation A/B (baseline vs. reflected)
# --------------------------------------------------------------------------


class InvestigationABCase(BaseModel):
    """One case for the baseline-vs-reflected Investigation Agent
    comparison. `evidence_texts` plus `query` are the ONLY inputs -- both
    runs (`agents.investigation.hypothesis.generate_hypotheses` directly
    for baseline, `agents.investigation.critique.review_investigation` for
    reflected) receive the exact same `EvidenceItem` list built from this
    case, guaranteeing equivalent inputs (this priority's explicit
    requirement) by construction rather than by matching two separately-
    gathered evidence sets after the fact.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    provenance: BenchmarkCaseProvenance
    query: str
    #: `(reference, source, summary)` triples -- deterministically turned
    #: into real `EvidenceItem`s by `investigation_ab.py`, the same shape
    #: `investigation.hypothesis.generate_hypotheses` already expects.
    evidence: list[tuple[str, str, str]]
    tags: list[str] = Field(default_factory=list)


#: The explicit outcome classification this priority's spec requires --
#: section 4's own list, verbatim as a constrained vocabulary rather than a
#: free-text label.
InvestigationABOutcome = Literal[
    "critique_improved",
    "critique_correctly_rejected",
    "critique_damaged",
    "critique_no_measurable_change",
    "critique_unavailable",
]


class InvestigationRunMetrics(BaseModel):
    """What both the baseline and the reflected run report, in the same
    shape, so they can be compared field-for-field."""

    model_config = ConfigDict(frozen=True)

    hypothesis_count: int
    #: Mean `RootCauseHypothesis.confidence` across produced hypotheses --
    #: `None` when zero hypotheses were produced (nothing to average).
    mean_confidence: float | None
    #: How many hypotheses cite at least one real evidence reference -- for
    #: the baseline this is always `hypothesis_count`
    #: (`_validate_hypotheses` already guarantees it at generation time);
    #: included on both sides anyway so the comparison is symmetric and
    #: self-explanatory without cross-referencing this docstring.
    cited_hypothesis_count: int
    review_status: str | None = None
    critique_verdict: str | None = None
    revision_count: int = 0
    latency_seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class InvestigationABResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    baseline: InvestigationRunMetrics
    reflected: InvestigationRunMetrics
    outcome: InvestigationABOutcome
    #: Short, structured explanation of why `outcome` was assigned this
    #: value -- e.g. "reflected rejected 2 unsupported baseline
    #: hypotheses, produced 0" -- never raw model reasoning.
    reason: str
    error: str | None = None


# --------------------------------------------------------------------------
# calibration
# --------------------------------------------------------------------------


class CalibrationCandidateResult(BaseModel):
    """One candidate value's measured behavior -- the sweep row this
    priority's spec describes ("threshold = 0.60 -> precision/recall")."""

    model_config = ConfigDict(frozen=True)

    candidate_value: float
    metrics: dict[str, float | None]


class CalibrationReport(BaseModel):
    """One threshold's full calibration picture: what was measured, across
    which candidates, and whether the sample size actually supports a
    conclusion."""

    model_config = ConfigDict(frozen=True)

    setting_name: str
    current_value: float
    description: str
    sample_size: int
    #: Below this, `status` can never be `"calibrated"` regardless of how
    #: clean the numbers look -- see `calibration.py`.
    minimum_sample_size: int
    candidates: list[CalibrationCandidateResult] = Field(default_factory=list)
    recommended_value: float | None = None
    status: CalibrationStatus
    #: Human-readable justification for `status` -- always present, since
    #: "provisional"/"insufficient_data" without a stated reason is exactly
    #: the unsupported-claim failure mode this package exists to prevent.
    rationale: str


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


#: Whether the generator and judge model were independent -- Priority 11
#: section 15. `get_llm()` (`app.agents.llm`) is a single global model
#: config; `runner.py` passes the SAME `llm` instance to both
#: `generate_answer_with_outcome` and `judge_answer_quality`, so the
#: automated portion of every report today is always `"same_model"` --
#: reported honestly, not glossed over. `"human_ground_truth"` is the
#: actual independence boundary this priority adds: a case compared
#: against a human annotation has a genuinely independent judge (a person,
#: not the model under test), regardless of what model generated the
#: answer.
EvaluatorIndependence = Literal[
    "same_model", "different_model_same_provider", "different_provider", "human_ground_truth"
]


class ExecutionMetadata(BaseModel):
    """Reproducibility metadata -- section 12's requirement. Never a
    secret value; `model_provider`/`model_name` only, never an API key."""

    model_config = ConfigDict(frozen=True)

    dataset_version: str
    model_provider: str | None
    model_name: str | None
    generated_at: datetime
    git_commit: str | None = None
    #: The automated (non-human) evaluator's independence from the
    #: generator -- see `EvaluatorIndependence`'s own docstring. Always
    #: `"same_model"` in this codebase's current configuration; kept as an
    #: explicit field (not hardcoded in a docstring) so a future multi-
    #: provider configuration would have somewhere real to report a
    #: different value, without this priority pretending one exists today.
    evaluator_independence: EvaluatorIndependence = "same_model"


class SemanticBenchmarkReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    execution: ExecutionMetadata
    #: `True` only when every category that was requested actually ran to
    #: completion (credentials present, no infrastructure failure). `False`
    #: means `verdict == "benchmark_execution_failed"` and every metric
    #: below should be treated as partial/absent, not as a quality result.
    execution_succeeded: bool
    execution_error: str | None = None

    answer_quality_results: list[AnswerQualityResult] = Field(default_factory=list)
    investigation_ab_results: list[InvestigationABResult] = Field(default_factory=list)
    calibration: list[CalibrationReport] = Field(default_factory=list)

    #: Priority 11 -- `None` when no human-annotatable cases were run this
    #: pass (report this honestly as "human validation unavailable" in the
    #: CLI, per section 17, never silently omitted).
    human_ground_truth_coverage: HumanGroundTruthCoverage | None = None
    inter_annotator_agreement: AgreementReport | None = None
    evaluator_validation: EvaluatorValidationReport | None = None

    total_latency_seconds: float = 0.0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    estimated_cost_usd: float | None = None

    verdict: BenchmarkVerdict
    verdict_reason: str


# --------------------------------------------------------------------------
# human ground truth (Priority 11)
#
# Direction is one-way, always (module docstring's whole point, restated as
# a type-level fact): AnnotationDecision -> CaseGroundTruth -> compared
# against AnswerQualityResult by evaluator_validation.py. Nothing in this
# section is ever written BY the LLM evaluator, and nothing here is ever
# mutated once recorded -- see annotation_store.py's append-only contract.
# --------------------------------------------------------------------------

#: Distinguishes a real independent human judgment from an annotation this
#: package's own tooling produced to validate the MECHANISM (schema
#: validation, agreement math, evaluator-comparison plumbing) end to end.
#: Section 22's explicit requirement: mechanism-validating annotations MUST
#: be labelled `"synthetic_controlled_annotation"`, never presented as
#: external human validation. `"human_review"` is reserved for an actual
#: person's judgment recorded via `scripts/annotate_semantic_cases.py` --
#: this package never assigns that value itself; a caller of the CLI does,
#: by choosing to run it.
AnnotationProvenance = Literal["synthetic_controlled_annotation", "human_review"]

#: Categorical, not numeric (section 4: "do not require humans to provide
#: numeric scores if categorical judgments are more reproducible") --
#: three ordered levels are enough to be useful for qualitative review
#: without inviting false precision ("was this a 6 or a 7 out of 10") a
#: single reviewer cannot reproduce reliably.
DimensionRating = Literal["good", "acceptable", "poor"]

#: Every ground-truth case ends up in exactly one of these states -- see
#: `ground_truth.py`'s `resolve_ground_truth` for the derivation. Only
#: `agreed_review` and `resolved_disagreement` (and, with lower confidence,
#: `single_review`) ever produce a usable final label;
#: `unresolved_disagreement` NEVER does (section 7's explicit requirement),
#: represented here by `CaseGroundTruth.final_observed_mode`/
#: `final_outcome` being `None` in exactly that one state.
GroundTruthStatus = Literal[
    "single_review", "agreed_review", "resolved_disagreement", "unresolved_disagreement"
]


class AnnotationDecision(BaseModel):
    """One independent reviewer's judgment on one specific (case, candidate
    answer) pair -- immutable once written (`annotation_store.py` never
    overwrites an existing record; see that module's docstring).

    `observed_mode` uses the EXACT `ObservedAnswerMode` vocabulary the
    automated evaluator itself uses -- not a separate human vocabulary --
    a deliberate design choice: it is the same underlying question ("what
    did this candidate text actually do"), and a human's `outcome_
    correctness` is DERIVED from it via `outcome.classify_outcome_
    correctness(case.expected_answer_mode, observed_mode)`, the identical
    pure function the automated evaluator's own outcome is derived through
    -- never independently hand-picked by the reviewer. This holds humans
    and the evaluator to the exact same decision rule (see `outcome.py`'s
    own "why outcome-correctness is a pure lookup" reasoning) rather than
    risking a human and the code silently drifting on what "critical_
    failure" means.

    `case_snapshot_hash` binds this annotation to the EXACT question/
    evidence/candidate-answer triple the reviewer actually saw (section
    12's evidence-snapshot requirement) -- see
    `annotation_store.compute_case_snapshot_hash`. A later comparison
    against a run whose case content hashes differently must treat this
    annotation as stale (not applicable to that run), never silently
    compare it against different evidence or a different generated answer.
    """

    model_config = ConfigDict(frozen=True)

    case_id: str
    dataset_version: str
    #: Bumped whenever this contract's own shape changes in a way that
    #: would make an old annotation ambiguous to interpret -- e.g. adding a
    #: new required label. Recorded on every annotation so a future reader
    #: knows which rules applied when this one was written.
    annotation_schema_version: str = "annotation-v1"
    case_snapshot_hash: str
    #: Pseudonymous by design (section 5: "avoid unnecessarily exposing
    #: reviewer identity publicly") -- a short reviewer-chosen handle
    #: (e.g. `"reviewer-a"`), never a real name/email. This package does
    #: not resolve it against `app.core.users` -- benchmark ground truth is
    #: not a production, organization-scoped resource (see
    #: `docs/SEMANTIC_BENCHMARK.md`'s "Why not database-backed" for the
    #: full reasoning), so there is no tenant/user row for it to reference.
    reviewer_id: str
    provenance: AnnotationProvenance
    annotated_at: datetime
    observed_mode: ObservedAnswerMode
    #: Optional, categorical, per-dimension notes -- keys drawn from
    #: whichever rubric applies to `observed_mode` (the substantive four or
    #: the refusal four, see `schemas.py`'s own rubric classes). Never
    #: required: a reviewer confident about the mode/outcome but unsure how
    #: to rate e.g. "usefulness" should leave it out, not guess.
    dimension_ratings: dict[str, DimensionRating] = Field(default_factory=dict)
    #: A short, free-text justification -- required, since an unexplained
    #: label is exactly the kind of unsupported claim this whole benchmark
    #: exists to catch, now applied to the humans checking it too.
    rationale: str
    #: A reviewer can flag a case as unusable for calibration purposes
    #: (e.g. the evidence itself turned out to be malformed) without that
    #: silently vanishing from the record -- the annotation is still kept,
    #: just excluded downstream. See `ground_truth.py`.
    usable_for_calibration: bool = True

    #: `AnnotationDecision` alone cannot derive `outcome_correctness` --
    #: that needs the CASE's own `expected_answer_mode`, which this record
    #: doesn't carry (an annotation is about one candidate answer, not a
    #: restatement of the case it belongs to). See `ground_truth.
    #: derive_outcome_for_annotation`, which takes both and applies the
    #: same `outcome.classify_outcome_correctness` the automated evaluator
    #: uses.


class ResolutionAnnotation(AnnotationDecision):
    """A resolver's judgment on a case where two independent annotations
    disagreed -- structurally identical to `AnnotationDecision` (same
    labels, same required rationale) PLUS a pointer to exactly which
    annotations it resolves. Recorded as its OWN new record, appended, not
    a mutation of either original -- `resolved_annotation_ids` lets a
    future reader reconstruct "what did the two reviewers say, and what did
    the resolver decide, and why" in full, forever (section 6's
    immutability requirement)."""

    model_config = ConfigDict(frozen=True)

    resolved_annotation_ids: list[str]  # `f"{reviewer_id}:{annotated_at.isoformat()}"` pairs


class CaseGroundTruth(BaseModel):
    """The resolved state of ALL annotations recorded for one (case,
    dataset_version) pair -- computed fresh from `annotations` by
    `ground_truth.resolve_ground_truth`, never itself hand-edited or
    persisted as the source of truth (the individual `AnnotationDecision`
    records are; this is a view over them, reproducible from them at any
    time)."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    dataset_version: str
    annotations: list[AnnotationDecision]
    resolution: ResolutionAnnotation | None = None
    status: GroundTruthStatus
    #: `None` ONLY when `status == "unresolved_disagreement"` -- section
    #: 7's explicit requirement that unresolved disagreement never
    #: silently becomes usable truth.
    final_observed_mode: ObservedAnswerMode | None
    final_outcome: AnswerOutcomeCorrectness | None


class AgreementReport(BaseModel):
    """Inter-annotator agreement across every double-(or more-)reviewed
    case in one dataset version -- section 8. Never claims a statistic the
    sample doesn't support: `status == "insufficient_data"` means exactly
    that, and `cohens_kappa` stays `None` in that case rather than being
    computed and hidden behind a caveat nobody reads."""

    model_config = ConfigDict(frozen=True)

    dataset_version: str
    annotation_schema_version: str
    reviewed_case_count: int
    double_reviewed_case_count: int
    agreed_case_count: int
    disagreed_case_count: int
    unresolved_disagreement_count: int
    raw_agreement_rate: float | None
    #: Only computed when every double-reviewed case has EXACTLY two
    #: independent reviewers (kappa's own precondition) and
    #: `double_reviewed_case_count >= minimum_sample_size` -- `None`
    #: otherwise, never a number computed on too few cases and reported as
    #: if it meant something.
    cohens_kappa: float | None
    minimum_sample_size: int
    status: Literal["computed", "insufficient_data"]
    rationale: str


class ConfusionMatrixCell(BaseModel):
    model_config = ConfigDict(frozen=True)

    human_label: str
    evaluator_label: str
    count: int


class ClassMetric(BaseModel):
    """One class's precision/recall/F1 -- `None` for any metric whose
    denominator is zero, the same "never a silently wrong 0.0" convention
    `calibration.binary_precision_recall` already uses."""

    model_config = ConfigDict(frozen=True)

    label: str
    support: int  # how many human-labelled cases actually have this label
    precision: float | None
    recall: float | None
    f1: float | None


class SevereDisagreement(BaseModel):
    """One case where the evaluator's mistake is not just "wrong" but
    dangerous by this benchmark's own severity model (section 10) -- e.g.
    the evaluator called a hallucination `correct`. Surfaced individually,
    never buried inside an aggregate accuracy number."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    human_outcome: AnswerOutcomeCorrectness
    evaluator_outcome: AnswerOutcomeCorrectness
    severity: Literal["critical", "high"]
    #: Why this specific (human, evaluator) pair is dangerous, not merely
    #: wrong -- e.g. "evaluator called a hallucination correct."
    explanation: str


class EvaluatorValidationReport(BaseModel):
    """The comparison this priority's whole point is: automated evaluator
    output vs. human ground truth, computed deterministically (section 9:
    "Do not ask an LLM to judge whether its own output matches human
    labels" -- this whole module is plain Python comparison logic, no LLM
    call anywhere in it)."""

    model_config = ConfigDict(frozen=True)

    dataset_version: str
    sample_size: int
    minimum_sample_size: int
    answer_mode_agreement_rate: float | None
    outcome_agreement_rate: float | None
    outcome_confusion_matrix: list[ConfusionMatrixCell]
    outcome_class_metrics: list[ClassMetric]
    severe_disagreements: list[SevereDisagreement]
    #: Cases whose `CaseGroundTruth.status == "unresolved_disagreement"`
    #: (excluded from every statistic above) or whose evidence/answer
    #: hashed differently than what was annotated (stale) -- counted, not
    #: silently dropped.
    excluded_case_count: int
    status: Literal["computed", "insufficient_data"]
    rationale: str


class HumanGroundTruthCoverage(BaseModel):
    """Report section: how much of the corpus has real ground truth behind
    it, and of what kind -- section 16's "Human Ground Truth Coverage"."""

    model_config = ConfigDict(frozen=True)

    dataset_version: str
    annotation_schema_version: str
    total_cases: int
    annotated_cases: int
    double_reviewed_cases: int
    agreed_cases: int
    unresolved_disagreements: int
    #: Counted separately per Priority 11 section 21 -- never combined into
    #: one misleading "benchmark quality" number.
    provenance_counts: dict[str, int]
    eligible_for_evaluator_validation: int
