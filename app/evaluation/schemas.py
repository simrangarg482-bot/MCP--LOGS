"""Pydantic contracts for `app.evaluation` -- the on-disk dataset/case format,
and the in-memory result/report shapes the runner and reporters share.

Owned by: app/evaluation. Mirrors this codebase's own "one `schemas.py` per
module" convention (`app.retrieval.schemas`, `app.ingestion.schemas`,
`app.agents.schemas`) rather than splitting dataset-shape and result-shape
definitions into separate files -- there is no cross-module boundary here to
justify the split the way e.g. `app.shared.schemas.agent_contracts` is split
out from `app.agents.schemas` (that split exists because those types are
genuinely produced by one module and consumed by others; every type below is
both produced and consumed within `app.evaluation`).

Field-naming note: the original evaluation-case sketch this package was
built from used `identity: {user_id, roles, project_permissions}`. `roles`
was replaced with `permissions` here -- resolving roles to permission codes
is a real RBAC/database concern (`core.users.service.resolve_identity`),
which a fixture-driven deterministic evaluation case cannot invoke without
either a live database or re-implementing that resolution logic (exactly the
"evaluator becomes a second implementation of the system" anti-pattern this
package exists to avoid). Specifying `permissions` directly lets a case
build a real `app.shared.schemas.Identity` and pass it through the actual
`SearchFilters`/`has_permission` machinery unmodified.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.shared.schemas import ActorKind, Identity

# --------------------------------------------------------------------------
# mode / category vocabulary
# --------------------------------------------------------------------------

#: Mode 1 (default, CI-safe, no external API/DB), Mode 2 (real retrieval/DB,
#: no paid LLM required), Mode 3 (opt-in, real model calls). See
#: `app.evaluation.runner`'s module docstring for exactly what each mode
#: wires up.
EvaluationMode = Literal["deterministic", "integration", "live"]

EvaluationCategory = Literal[
    "retrieval", "grounding", "answer", "investigation", "memory", "graph", "proactive"
]

#: The core distinction this package exists to make explicit (see package
#: docstring / the "Bad Answer" decision-tree this schema was designed
#: against): a failing case is EITHER a retrieval failure (the evidence
#: needed was never fetched) OR a generation/reasoning failure (the evidence
#: was there, but the system didn't use it correctly) OR neither (it passed).
#: Never both, never unset on a failing result -- `EvaluationResult`'s own
#: validator (below) enforces this.
#:
#: Deliberately only two real failure stages plus "none". A broader
#: vocabulary (`grounding`/`investigation`/`assertion`) was considered and
#: rejected: `app.evaluation.runner` never produces those values, and every
#: category's failure genuinely reduces to "the evidence was never
#: retrieved" vs. "the evidence was there and the system still got it
#: wrong" -- which is the whole point of the distinction. Declaring stages
#: nothing can emit would be a fictional contract (this repo's own standing
#: rule: never invent a value to make a schema look richer than the code).
#: The case's `category` already records *which* evaluator ran; `stage`
#: records *where in the pipeline* it broke. Those are different questions.
FailureStage = Literal["retrieval", "generation", "none"]

#: Whether a dataset case is predicted to satisfy its own assertions
#: ("pass") or to be caught failing them ("fail" -- a negative control).
#: This is what turns the suite into a real regression gate: a negative
#: control that starts passing is as much a regression as a positive case
#: that starts failing, and neither is detectable from a raw pass count.
ExpectedOutcomeLabel = Literal["pass", "fail"]

_DEFAULT_DATASET_VERSION = "1.0"


# --------------------------------------------------------------------------
# dataset case format (on disk: one JSON object per line, JSONL)
# --------------------------------------------------------------------------


class EvalIdentity(BaseModel):
    """The evaluation-case identity block. Builds a real `Identity` via
    `to_identity()` rather than being consumed as a loose dict, so every
    adapter that needs permission-aware behavior (retrieval filtering,
    `has_permission` checks) goes through the actual production type.
    """

    model_config = ConfigDict(frozen=True)

    user_id: str = "eval-user"
    permissions: frozenset[str] = Field(default_factory=frozenset)
    project_permissions: dict[uuid.UUID, frozenset[str]] = Field(default_factory=dict)

    def to_identity(self, organization_id: uuid.UUID) -> Identity:
        return Identity(
            kind=ActorKind.USER,
            subject=self.user_id,
            organization_id=organization_id,
            permissions=self.permissions,
            project_permissions=self.project_permissions,
        )


class CitationExpectation(BaseModel):
    model_config = ConfigDict(frozen=True)

    minimum: int = 0
    must_support_answer: bool = True


class ConfidenceExpectation(BaseModel):
    model_config = ConfigDict(frozen=True)

    min: float | None = None
    max: float | None = None

    def satisfied_by(self, confidence: float | None) -> bool:
        if confidence is None:
            return False
        above_min = self.min is None or confidence >= self.min
        below_max = self.max is None or confidence <= self.max
        return above_min and below_max


class AnswerAssertion(BaseModel):
    """One deterministic (or optionally semantic) check against generated
    answer text. See `app.evaluation.assertions.answer` for the evaluation
    logic -- this is just the declared expectation.

    `value` is a single string for every type except `contains_any`/
    `contains_all`, which take a list. `threshold` is only meaningful for
    `semantic_similarity` (default similarity cutoff otherwise applies --
    see that assertion's own module).
    """

    model_config = ConfigDict(frozen=True)

    type: Literal[
        "exact_match",
        "contains",
        "contains_any",
        "contains_all",
        "forbidden_content",
        "regex",
        "semantic_similarity",
    ]
    value: str | list[str]
    threshold: float | None = None

    @field_validator("value")
    @classmethod
    def _value_shape_matches_type(cls, value: str | list[str], info: Any) -> str | list[str]:
        assertion_type = info.data.get("type")
        needs_list = assertion_type in ("contains_any", "contains_all")
        if needs_list and not isinstance(value, list):
            raise ValueError(f"assertion type {assertion_type!r} requires a list value")
        if not needs_list and isinstance(value, list):
            raise ValueError(f"assertion type {assertion_type!r} requires a single string value")
        return value


class ExpectedHypothesis(BaseModel):
    """One hypothesis a case expects the Investigation Agent to have
    produced -- see `app.shared.schemas.agent_contracts.RootCauseHypothesis`
    for the real, agent-produced shape this is graded against.
    """

    model_config = ConfigDict(frozen=True)

    concept: str
    required_evidence_ids: list[str] = Field(default_factory=list)
    minimum_support: int = 1


class ExpectedCritique(BaseModel):
    """What a `category="investigation"` case expects Priority 7's bounded
    critique stage (`agents.investigation.critique`) to conclude, if the
    case populates this block at all -- most investigation cases don't (see
    `ExpectedInvestigation.critique`'s own docstring).
    """

    model_config = ConfigDict(frozen=True)

    #: `None` means "don't check the verdict" (used when only
    #: `expect_review_failed` matters for this case).
    expected_verdict: Literal["accept", "revise", "reject"] | None = None
    #: When true, the case expects critique to have failed to complete
    #: (`review_status == "review_failed"`) -- mutually exclusive in
    #: practice with `expected_verdict`, since a completed critique always
    #: has a verdict and a failed one never does.
    expect_review_failed: bool = False


class ExpectedInvestigation(BaseModel):
    model_config = ConfigDict(frozen=True)

    hypotheses: list[ExpectedHypothesis] = Field(default_factory=list)
    required_evidence_ids: list[str] = Field(default_factory=list)
    #: `None` (the default) means this case does not exercise critique at
    #: all -- `_run_investigation_case` skips the check entirely, matching
    #: every pre-Priority-7 investigation case's behavior unchanged.
    critique: ExpectedCritique | None = None


class ExpectedMemoryRecall(BaseModel):
    """What a `category="memory"` case expects `core.memory` to recall.

    Memory fixtures are referenced by short readable LABEL, not by UUID:
    unlike documents (whose ids the fixture corpus derives deterministically),
    memories in an evaluation dataset are created by the fixture store at run
    time, so a label is the only stable handle. See
    `app.evaluation.fixtures.memory_corpus`.
    """

    model_config = ConfigDict(frozen=True)

    #: Labels that MUST be recalled for this query.
    expected_labels: list[str] = Field(default_factory=list)
    #: Labels that must NOT be recalled -- irrelevant memories, another
    #: user's private memories, superseded or deleted ones. This is the half
    #: that catches leaks, so a case with no `expected_labels` at all (pure
    #: negative control) is a legitimate and useful shape.
    forbidden_labels: list[str] = Field(default_factory=list)


class ExpectedGraphTraversal(BaseModel):
    """What a `category="graph"` case expects `core.graph` traversal from
    `EvaluationCase.origin_label` to reach.

    Graph fixtures are referenced by short readable LABEL, not by UUID --
    the same reasoning `ExpectedMemoryRecall`'s docstring gives for memory:
    entities in a fixture graph are constructed at run time, so a label is
    the only stable handle. See `app.evaluation.fixtures.graph_corpus`.
    """

    model_config = ConfigDict(frozen=True)

    #: Never exceeds `app.core.graph.schemas.MAX_TRAVERSAL_DEPTH` in a real
    #: run (the service clamps it regardless); the fixture adapter honors
    #: whatever a case declares, so a case testing the depth cap itself can
    #: still request more than the ceiling and expect it to make no
    #: difference.
    depth: int = 2
    #: Labels that MUST be reachable within `depth` hops.
    expected_labels: list[str] = Field(default_factory=list)
    #: Labels that must NOT be reachable -- another organization's entities,
    #: an unauthorized project's entities, or a deleted/unpublished one.
    #: This is the half that catches leaks, so a case with no
    #: `expected_labels` at all (pure negative control) is a legitimate and
    #: useful shape.
    forbidden_labels: list[str] = Field(default_factory=list)


class ExpectedProactiveFindings(BaseModel):
    """What a `category="proactive"` case expects `core.proactive` to
    surface to `EvaluationCase.identity`.

    Findings are referenced by short readable LABEL (standing in for a
    fingerprint), not a real id -- the same reasoning `ExpectedMemoryRecall`/
    `ExpectedGraphTraversal`'s own docstrings give: fixture findings have no
    database row, so a label is the only stable handle. See
    `app.evaluation.fixtures.proactive_corpus`.
    """

    model_config = ConfigDict(frozen=True)

    #: Labels that MUST be visible (i.e. would survive `core.proactive.
    #: service`'s authorization + mixed-visibility support recomputation).
    expected_labels: list[str] = Field(default_factory=list)
    #: Labels that must NOT be visible -- another organization's finding, an
    #: unauthorized project's finding, or one whose visible evidence falls
    #: below its own threshold once unauthorized evidence is excluded. This
    #: is the half that catches leaks, so a case with no `expected_labels`
    #: at all (pure negative control) is a legitimate and useful shape.
    forbidden_labels: list[str] = Field(default_factory=list)


class ExpectedOutcome(BaseModel):
    """Everything a case asserts about the system's behavior. Every field is
    optional and empty/absent by default -- a case only needs to populate
    the fields relevant to its own `category` (PROJECT_PLAN-style "exact
    answer text is never mandatory": `answer_assertions` is how a case opts
    into answer-text checking at all).
    """

    model_config = ConfigDict(frozen=True)

    relevant_document_ids: list[str] = Field(default_factory=list)
    required_concepts: list[str] = Field(default_factory=list)
    forbidden_concepts: list[str] = Field(default_factory=list)
    citations: CitationExpectation | None = None
    confidence: ConfidenceExpectation | None = None
    answer_assertions: list[AnswerAssertion] = Field(default_factory=list)
    investigation: ExpectedInvestigation | None = None
    memory: ExpectedMemoryRecall | None = None
    graph: ExpectedGraphTraversal | None = None
    proactive: ExpectedProactiveFindings | None = None


class EvaluationCase(BaseModel):
    """One JSONL line in a dataset file. `organization_id` is a plain string
    here, not a `uuid.UUID` -- fixture datasets use stable, readable slugs
    (`"test-org"`) rather than real UUIDs; adapters that need a real
    `uuid.UUID` (to build `SearchFilters`) derive one deterministically via
    `organization_uuid` below, so the same slug always maps to the same UUID
    within one evaluation run.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    category: EvaluationCategory
    query: str
    organization_id: str = "eval-org"
    identity: EvalIdentity = Field(default_factory=EvalIdentity)
    #: For `category="graph"` only: the fixture label of the entity to
    #: traverse from (`app.evaluation.fixtures.graph_corpus`). Unused by
    #: every other category -- `query` already serves that role for them,
    #: but a graph traversal has no free-text query to run, so it gets its
    #: own field rather than overloading `query`'s meaning.
    origin_label: str | None = None
    expected: ExpectedOutcome = Field(default_factory=ExpectedOutcome)
    tags: list[str] = Field(default_factory=list)
    #: Whether this case is predicted to satisfy `expected` ("pass") or to
    #: be caught violating it ("fail" -- a negative control). Defaults to
    #: "pass": the overwhelmingly common case, and the safe default (a
    #: newly-added case that starts failing should break the suite loudly,
    #: not be silently absorbed as "well, maybe it was meant to fail").
    expected_outcome: ExpectedOutcomeLabel = "pass"
    #: For `expected_outcome="fail"` only: which stage the failure must
    #: occur at. `None` means "fail somehow, stage unchecked". Pinning the
    #: stage is what stops a negative control from passing for the *wrong
    #: reason* -- e.g. a citation-fabrication control that starts failing at
    #: the `retrieval` stage instead is no longer testing citations at all,
    #: even though it is still, technically, failing.
    expected_failure_stage: FailureStage | None = None

    @field_validator("expected_failure_stage")
    @classmethod
    def _stage_only_meaningful_for_expected_failures(
        cls, stage: FailureStage | None, info: Any
    ) -> FailureStage | None:
        if stage is None:
            return stage
        if stage == "none":
            raise ValueError(
                "expected_failure_stage cannot be 'none' -- omit the field entirely to mean "
                "'fail somehow, stage unchecked'"
            )
        if info.data.get("expected_outcome") != "fail":
            raise ValueError(
                "expected_failure_stage is only meaningful when expected_outcome is 'fail'"
            )
        return stage

    @property
    def organization_uuid(self) -> uuid.UUID:
        """Deterministic UUID5 derived from `organization_id`, so the same
        slug always resolves to the same UUID across a run without needing
        a real `organizations` row -- fixture corpora (see
        `app.evaluation.fixtures`) are keyed by this same derivation.
        """
        return uuid.uuid5(uuid.NAMESPACE_DNS, f"ekip-eval:{self.organization_id}")


class DatasetMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    dataset_name: str
    version: str = _DEFAULT_DATASET_VERSION
    description: str = ""


class Dataset(BaseModel):
    model_config = ConfigDict(frozen=True)

    metadata: DatasetMetadata
    cases: list[EvaluationCase]


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------


class MetricResult(BaseModel):
    """One named, numeric measurement -- a metric's own unit test asserts
    against the bare `float`/`dict` a metric function returns; this wrapper
    is only for carrying a metric alongside its name/params once results are
    aggregated into a report.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    value: float | None
    details: dict[str, Any] = Field(default_factory=dict)


class FailureDetail(BaseModel):
    """The retrieval-vs-generation distinction this package's design centers
    on (see package docstring). `stage="none"` on a passing result --
    never omitted, so a report can always answer "what kind of failure was
    this" without a `None`-means-what-exactly ambiguity.
    """

    model_config = ConfigDict(frozen=True)

    stage: FailureStage
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_required_even_when_passing(cls, reason: str) -> str:
        if not reason:
            raise ValueError(
                "reason must be non-empty (use a short 'passed' description if stage=none)"
            )
        return reason


class EvaluationResult(BaseModel):
    """One case's outcome. `retrieved_document_ids` is always populated (even
    for non-retrieval categories, whenever the case's evaluation involved a
    retrieval step) specifically so a grounding/answer failure can still be
    triaged against what was actually retrieved -- the same evidence a human
    reviewer would want first when told "case X failed."

    **`passed` and `matched_expectation` are different questions, and the
    distinction is the whole basis of the CI gate.** `passed` answers "did
    the system satisfy this case's assertions." `matched_expectation`
    answers "did the system behave the way the dataset predicted." A
    negative control that correctly fails has `passed=False` but
    `matched_expectation=True` -- it is doing its job. CI gates on
    `matched_expectation`, never on `passed`, because gating on `passed`
    would make every negative control a permanent build failure (and
    deleting the controls to get green would remove exactly the cases that
    prove the evaluator detects anything at all).
    """

    model_config = ConfigDict(frozen=True)

    case_id: str
    category: EvaluationCategory
    mode: EvaluationMode
    passed: bool
    failure: FailureDetail
    #: Copied verbatim from the originating `EvaluationCase` so a result --
    #: and the JSON report built from it -- is self-contained: judging
    #: whether a run regressed never requires re-reading the dataset the
    #: run came from.
    expected_outcome: ExpectedOutcomeLabel = "pass"
    expected_failure_stage: FailureStage | None = None
    retrieved_document_ids: list[str] = Field(default_factory=list)
    predicted_confidence: float | None = None
    metrics: dict[str, MetricResult] = Field(default_factory=dict)
    duration_seconds: float = 0.0
    timestamp: datetime
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("failure")
    @classmethod
    def _stage_consistent_with_pass_fail(cls, failure: FailureDetail, info: Any) -> FailureDetail:
        passed = info.data.get("passed")
        if passed is True and failure.stage != "none":
            raise ValueError("a passing result must have failure.stage == 'none'")
        if passed is False and failure.stage == "none":
            raise ValueError(
                "a failing result must set failure.stage to 'retrieval' or 'generation'"
            )
        return failure

    @property
    def matched_expectation(self) -> bool:
        """Whether actual behavior matched what the dataset predicted -- the
        one property CI should ever gate on. See this class's docstring.
        """
        if self.expected_outcome == "pass":
            return self.passed
        if self.passed:
            return False  # a negative control that stopped detecting anything
        if self.expected_failure_stage is None:
            return True  # "fail somehow, stage unchecked"
        return self.failure.stage == self.expected_failure_stage

    @property
    def regression_kind(self) -> str | None:
        """A short, stable label for *how* this result defied expectation --
        `None` when it matched. Used by the console/JSON reports and by the
        CLI's exit-code decision, so the three genuinely different
        regressions never get flattened into one "N failed" number:

        - `unexpected_failure`  -- an expected-pass case failed. The classic
          regression: something that used to work broke.
        - `unexpected_pass`     -- a negative control passed. Equally a
          regression, and a nastier one: the evaluator has stopped catching
          a defect it was specifically built to catch, so the suite is now
          quietly weaker than its pass count suggests.
        - `wrong_failure_stage` -- a negative control failed, but somewhere
          other than where it was pinned. Still failing, no longer testing
          what it was written to test.
        """
        if self.matched_expectation:
            return None
        if self.expected_outcome == "pass":
            return "unexpected_failure"
        if self.passed:
            return "unexpected_pass"
        return "wrong_failure_stage"


class CalibrationBucket(BaseModel):
    model_config = ConfigDict(frozen=True)

    range_low: float
    range_high: float
    count: int
    mean_predicted_confidence: float | None
    actual_success_rate: float | None
    calibration_gap: float | None


class CalibrationReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    buckets: list[CalibrationBucket]
    #: Sample-weighted mean absolute calibration gap across all non-empty
    #: buckets -- "Expected Calibration Error" in the usual sense.
    overall_calibration_error: float | None
    sample_count: int


class EvaluationReport(BaseModel):
    """The full structured result of one evaluation run -- what
    `reporting.json_report`/`reporting.console` render."""

    model_config = ConfigDict(frozen=True)

    dataset_name: str
    dataset_version: str
    mode: EvaluationMode
    generated_at: datetime
    git_commit: str | None = None
    model_provider: str | None = None
    model_name: str | None = None
    results: list[EvaluationResult]
    aggregate_metrics: dict[str, MetricResult] = Field(default_factory=dict)
    calibration: CalibrationReport | None = None

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed_count(self) -> int:
        return self.total - self.passed_count

    @property
    def failures(self) -> list[EvaluationResult]:
        return [r for r in self.results if not r.passed]

    # --- expectation accounting (what CI actually gates on) ---------------

    @property
    def regressions(self) -> list[EvaluationResult]:
        """Every result that defied its dataset's prediction, in any of the
        three ways `EvaluationResult.regression_kind` distinguishes. An
        empty list is the only acceptable CI outcome."""
        return [r for r in self.results if not r.matched_expectation]

    @property
    def unexpected_failures(self) -> list[EvaluationResult]:
        return [r for r in self.results if r.regression_kind == "unexpected_failure"]

    @property
    def unexpected_passes(self) -> list[EvaluationResult]:
        return [r for r in self.results if r.regression_kind == "unexpected_pass"]

    @property
    def wrong_stage_failures(self) -> list[EvaluationResult]:
        return [r for r in self.results if r.regression_kind == "wrong_failure_stage"]

    @property
    def expected_failure_count(self) -> int:
        """Negative controls that correctly failed -- reported so the
        console output can say "10 negative controls detected as expected"
        instead of leaving a reader to wonder why 10 cases are red in a
        green build."""
        return sum(
            1 for r in self.results if r.expected_outcome == "fail" and r.matched_expectation
        )

    @property
    def is_clean(self) -> bool:
        """True when every case behaved exactly as predicted. This -- not
        `failed_count == 0` -- is the CI gate."""
        return not self.regressions


_WHITESPACE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Shared normalization for text comparisons across assertions/grounding
    -- lowercase, collapsed whitespace, stripped. Deliberately not stemming
    or removing punctuation: exact/contains-family assertions should stay
    predictable, not linguistically fuzzy (that is `semantic_similarity`'s
    job, a distinct assertion type).
    """
    return _WHITESPACE.sub(" ", text).strip().lower()
