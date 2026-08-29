"""The benchmark corpus for `app.evaluation.semantic` -- Tier 3.

DELIBERATELY SMALL, DELIBERATELY LABELLED (section 10)
    `SYNTHETIC_ANSWER_QUALITY_CASES`/`SYNTHETIC_INVESTIGATION_AB_CASES`
    below are hand-authored, self-contained (no database required), and
    marked `provenance="synthetic_controlled"` -- sufficient to validate
    that the benchmark ENGINE runs correctly end to end (dataset loading,
    evaluator calls, metric aggregation, reporting), but explicitly NOT a
    claim of empirical production calibration. Do not read a clean result
    against this corpus as evidence the underlying system performs well in
    production; it is evidence the benchmark harness itself works.

    A second, `provenance="repository_derived"` source
    (`load_repository_derived_answer_quality_cases`) is also provided --
    it reuses `scripts/eval_confidence_dataset.json`'s real questions
    (already validated against `test-org`'s real ingested GitHub/Slack
    content by `scripts/eval_confidence.py`/`tests/rag_validation`) rather
    than inventing a second "realistic-looking" dataset. It requires a live
    database with that data actually ingested and is not runnable without
    one -- see `runner.py`.

Adding a future sanitized production corpus means adding another loader
function of this same shape (returning `list[AnswerQualityCase]` /
`list[InvestigationABCase]`) -- the benchmark engine itself
(`answer_quality.py`/`investigation_ab.py`/`runner.py`) does not change.

CONTRAST CASES (Priority 9 section 6) -- `CONTRAST_ANSWER_QUALITY_CASES`
    Six cases, three same-question/same-evidence PAIRS, each pair differing
    only in `fixed_answer` and, correspondingly, in what the correct
    evaluation should be:
      - `contrast-a-correct-answer` / `contrast-c-hallucination`: identical
        insufficient-evidence setup, one a correct refusal, one a
        fabricated substantive answer -- a hallucination must score
        materially worse than the correct refusal it's paired against, not
        better for "sounding complete."
      - `contrast-b-correct-answer` / `contrast-d-incorrect-refusal`:
        identical strong-evidence setup (reusing `aq-pool-exhaustion-clear`'s
        own evidence), one a correct substantive answer, one section 4's own
        worked example of a lazy refusal against evidence that clearly
        answers the question. `contrast-a`/`contrast-d`'s refusal text is
        deliberately the SAME real production sentinel
        (`agents.answer.node._INSUFFICIENT_GROUNDING_MESSAGE`, reused
        verbatim, not an invented phrase) against two different evidence
        sets -- this is precisely what tests `abstention_correctness`'s
        discrimination: the judge must reason about the EVIDENCE, not the
        refusal's own wording (which is identical in both cases), to tell a
        justified decline from a lazy one.
      - `contrast-e-qualified` / `contrast-f-overconfident`: identical
        conflicting/partial evidence, one appropriately hedges, one states
        one side of the conflict with full, unwarranted confidence.
    These exist to test the EVALUATOR's discrimination (`outcome.py` +
    `answer_quality.py`), not to measure production quality -- see
    `docs/SEMANTIC_BENCHMARK.md`'s "Evaluator discrimination" section for
    what running them actually showed.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.agents.answer.node import _INSUFFICIENT_GROUNDING_MESSAGE
from app.evaluation.semantic.schemas import AnswerQualityCase, InvestigationABCase

DATASET_VERSION = "semantic-v1"

_EVAL_CONFIDENCE_DATASET_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "eval_confidence_dataset.json"
)

# --------------------------------------------------------------------------
# synthetic, controlled -- self-contained, no database required
# --------------------------------------------------------------------------

SYNTHETIC_ANSWER_QUALITY_CASES: list[AnswerQualityCase] = [
    AnswerQualityCase(
        id="aq-pool-exhaustion-clear",
        provenance="synthetic_controlled",
        question="Why did the authentication service go down?",
        evidence_texts=[
            "Deployment 456 reduced DB_POOL_SIZE from 100 to 10 at 14:02 UTC.",
            "Incident timeline: auth service began returning 500s at 14:05 UTC, three "
            "minutes after deployment 456 completed.",
            "Postmortem root cause: connection pool exhaustion under normal login load "
            "once DB_POOL_SIZE was reduced to 10.",
        ],
        reference_answer=(
            "The authentication service went down because deployment 456 reduced "
            "DB_POOL_SIZE from 100 to 10, causing connection pool exhaustion under "
            "normal load."
        ),
        expected_answer_mode="answer",
        tags=["clear", "grounded"],
    ),
    AnswerQualityCase(
        id="aq-unrelated-question",
        provenance="synthetic_controlled",
        question="What is the capital of France?",
        evidence_texts=[
            "Deployment 456 reduced DB_POOL_SIZE from 100 to 10 at 14:02 UTC.",
        ],
        reference_answer=None,
        expected_answer_mode="no_answer",
        tags=["out-of-domain", "negative"],
    ),
    AnswerQualityCase(
        id="aq-partial-evidence",
        provenance="synthetic_controlled",
        question="What caused the checkout service outage and how was it fixed?",
        evidence_texts=[
            "Checkout service latency spiked at 09:14 UTC; root cause was never "
            "conclusively identified in the available postmortem notes.",
        ],
        reference_answer=None,
        expected_answer_mode="no_answer",
        tags=["insufficient-evidence"],
    ),
]

# --------------------------------------------------------------------------
# contrast cases -- test evaluator discrimination, not production quality
# (Priority 9 section 6; see module docstring)
# --------------------------------------------------------------------------

_INSUFFICIENT_EVIDENCE = [
    "Checkout service experienced elevated latency starting at 09:14 UTC.",
    "The on-call engineer restarted the checkout pods at 09:30 UTC; latency "
    "returned to normal by 09:35 UTC.",
    "No root cause was identified in the retained logs for this incident.",
]

_STRONG_EVIDENCE = SYNTHETIC_ANSWER_QUALITY_CASES[0].evidence_texts  # aq-pool-exhaustion-clear's

_CONFLICTING_EVIDENCE = [
    "Commit a1b2c3 pins the notification-worker service to Python 3.10 "
    '("lock notification-worker to py3.10 for the legacy SDK").',
    "Commit d4e5f6, three months later, updates the BASE IMAGE for the "
    "unrelated billing-worker service to Python 3.11.",
]

CONTRAST_ANSWER_QUALITY_CASES: list[AnswerQualityCase] = [
    # A / C: same insufficient-evidence question, correct refusal vs hallucination.
    AnswerQualityCase(
        id="contrast-a-correct-refusal",
        provenance="synthetic_controlled",
        question="What was the root cause of the checkout service latency incident?",
        evidence_texts=_INSUFFICIENT_EVIDENCE,
        expected_answer_mode="no_answer",
        fixed_answer=_INSUFFICIENT_GROUNDING_MESSAGE,
        tags=["contrast", "correct-refusal"],
    ),
    AnswerQualityCase(
        id="contrast-c-hallucination",
        provenance="synthetic_controlled",
        question="What was the root cause of the checkout service latency incident?",
        evidence_texts=_INSUFFICIENT_EVIDENCE,
        expected_answer_mode="no_answer",
        fixed_answer=(
            "The checkout latency incident was caused by a memory leak in the payment "
            "gateway client introduced by deployment 512, which was later patched."
        ),
        tags=["contrast", "hallucination"],
    ),
    # B / D: same strong-evidence question, correct answer vs incorrect (lazy) refusal.
    AnswerQualityCase(
        id="contrast-b-correct-answer",
        provenance="synthetic_controlled",
        question="Why did the authentication service go down?",
        evidence_texts=_STRONG_EVIDENCE,
        expected_answer_mode="answer",
        fixed_answer=(
            "The authentication service went down because deployment 456 reduced "
            "DB_POOL_SIZE from 100 to 10, causing connection pool exhaustion under "
            "normal login load."
        ),
        tags=["contrast", "correct-answer"],
    ),
    AnswerQualityCase(
        id="contrast-d-incorrect-refusal",
        provenance="synthetic_controlled",
        question="Why did the authentication service go down?",
        evidence_texts=_STRONG_EVIDENCE,
        expected_answer_mode="answer",
        fixed_answer=_INSUFFICIENT_GROUNDING_MESSAGE,
        tags=["contrast", "incorrect-refusal"],
    ),
    # E / F: same conflicting/partial evidence, appropriate qualification vs overconfidence.
    AnswerQualityCase(
        id="contrast-e-qualified",
        provenance="synthetic_controlled",
        question="What Python version does the notification-worker service require?",
        evidence_texts=_CONFLICTING_EVIDENCE,
        expected_answer_mode="qualified_answer",
        fixed_answer=(
            "Based on the available evidence, notification-worker was pinned to Python "
            "3.10 in one commit. A later commit updates a different service (billing-"
            "worker) to Python 3.11 -- I can't confirm whether that change also applies "
            "to notification-worker without more context."
        ),
        tags=["contrast", "qualified"],
    ),
    AnswerQualityCase(
        id="contrast-f-overconfident",
        provenance="synthetic_controlled",
        question="What Python version does the notification-worker service require?",
        evidence_texts=_CONFLICTING_EVIDENCE,
        expected_answer_mode="qualified_answer",
        fixed_answer="The notification-worker service requires Python 3.11.",
        tags=["contrast", "overconfident"],
    ),
]

SYNTHETIC_INVESTIGATION_AB_CASES: list[InvestigationABCase] = [
    InvestigationABCase(
        id="iab-well-supported",
        provenance="synthetic_controlled",
        query="Investigate the checkout service outage",
        evidence=[
            (
                "deploy-901",
                "deployment",
                "Deployment 901 changed the payment gateway timeout from 30s to 3s.",
            ),
            (
                "incident-77",
                "postmortem",
                "Checkout service outage root-caused to the payment gateway timeout "
                "reduction in deployment 901, which caused legitimate slow-but-valid "
                "gateway calls to be aborted.",
            ),
        ],
        tags=["well-supported"],
    ),
    InvestigationABCase(
        id="iab-thin-evidence",
        provenance="synthetic_controlled",
        query="Investigate why nightly batch jobs are failing",
        evidence=[
            ("slack-42", "slack", "Someone mentioned batch jobs look slow lately, not sure why."),
        ],
        tags=["thin-evidence", "expect-critique-flag"],
    ),
    InvestigationABCase(
        id="iab-no-evidence",
        provenance="synthetic_controlled",
        query="Investigate a report of intermittent 500 errors with no further detail",
        evidence=[],
        tags=["no-evidence", "expect-empty"],
    ),
]


# --------------------------------------------------------------------------
# repository-derived -- reuses eval_confidence_dataset.json's real questions
# --------------------------------------------------------------------------


#: `scripts/eval_confidence_dataset.json` category -> this benchmark's
#: `ExpectedAnswerMode` (Priority 9). Grounded directly in what each
#: category already means to `scripts/eval_confidence.py` and to the
#: production `SufficiencyVerdict` (`app.agents.answer.sufficiency`) this
#: dataset's own motivating bug (that module's docstring) was built around:
#:   "clear-answer"   -- some evidence states a specific, direct answer
#:                       (`SufficiencyVerdict="sufficient"`) -> "answer".
#:   "ambiguous"       -- evidence is topically relevant but silent on the
#:                       specific fact, or conflicting
#:                       (`SufficiencyVerdict="partial"`) -> "qualified_answer".
#:                       `eval_confidence.py` already scores confidently
#:                       answering these as WRONG; declaring them
#:                       "qualified_answer" here (not "no_answer") keeps
#:                       that same judgment while giving credit for a
#:                       hedge, not only for a flat refusal.
#:   "no-information"  -- evidence has no real bearing on the question at
#:                       all (`SufficiencyVerdict="insufficient"`) -> "no_answer".
_CATEGORY_TO_EXPECTED_MODE: dict[str, str] = {
    "clear-answer": "answer",
    "ambiguous": "qualified_answer",
    "no-information": "no_answer",
}


def load_repository_derived_answer_quality_cases(
    limit: int | None = None,
) -> list[AnswerQualityCase]:
    """Load questions from `scripts/eval_confidence_dataset.json` as
    answer-quality cases with NO pre-supplied evidence text --
    `runner.py`'s repository-derived mode runs real retrieval against
    `test-org`'s real ingested data to fill `evidence_texts` in, then
    generates a real answer, then judges it.

    Every one of this dataset's three categories (`clear-answer`,
    `ambiguous`, `no-information`) is loaded and labelled with its
    corresponding `expected_answer_mode` via `_CATEGORY_TO_EXPECTED_MODE`
    -- extended in Priority 9 from Priority 8's `clear-answer`-only
    behavior, now that this benchmark can score a correct qualification or
    refusal instead of only ever scoring a substantive answer. Any question
    in a category this table doesn't recognize is loaded as
    `expected_answer_mode="unlabeled"` rather than guessed (section 5) --
    unreachable today (the dataset only has these three categories) but a
    deliberate safety net against a silently-wrong label if a fourth
    category is ever added to `eval_confidence_dataset.json` without a
    matching update here.

    `limit`, when given, caps the count PER CATEGORY, not the total -- so a
    small `--limit` still exercises all three expected modes rather than
    only ever loading `clear-answer` questions first.

    Returns `[]` (not an error) if the dataset file doesn't exist --
    callers treat that as "repository-derived mode unavailable," the same
    honest-empty convention `core.graph.service`'s discovery pass uses when
    there's nothing to scan.
    """
    if not _EVAL_CONFIDENCE_DATASET_PATH.exists():
        return []
    raw = json.loads(_EVAL_CONFIDENCE_DATASET_PATH.read_text(encoding="utf-8"))
    all_questions = raw.get("questions", [])

    #: Every category actually present in the dataset, not just the three
    #: `_CATEGORY_TO_EXPECTED_MODE` currently recognizes -- an unrecognized
    #: category is still loaded (as `"unlabeled"`, via the `.get(...,
    #: "unlabeled")` below), never silently dropped. `dict.fromkeys` rather
    #: than `set(...)` to keep a stable, deterministic category order.
    categories = list(dict.fromkeys(q.get("category") for q in all_questions if q.get("category")))

    cases: list[AnswerQualityCase] = []
    for category in categories:
        questions = [q for q in all_questions if q.get("category") == category]
        if limit is not None:
            questions = questions[:limit]
        expected_mode = _CATEGORY_TO_EXPECTED_MODE.get(category, "unlabeled")
        cases.extend(
            AnswerQualityCase(
                id=f"repo-{q['id']}",
                provenance="repository_derived",
                question=q["question"],
                evidence_texts=[],  # filled in by the runner via real retrieval
                reference_answer=None,
                expected_answer_mode=expected_mode,
                tags=["repository-derived", "test-org", category],
            )
            for q in questions
        )
    return cases


# --------------------------------------------------------------------------
# human-annotatable corpus (Priority 11) -- every case here has a FIXED,
# deterministic (question, evidence, candidate answer) triple, so a human
# annotation stays valid across repeated benchmark runs. See
# `docs/SEMANTIC_BENCHMARK.md`'s "Human-annotatable corpus" section for why
# this deliberately excludes both live-generated cases (the candidate text
# changes every run, so an annotation of it goes stale immediately) and
# `--repository-derived`'s live-retrieval mode (evidence itself can drift).
# --------------------------------------------------------------------------


def _load_dataset_questions_by_id(*ids: str) -> dict[str, dict]:
    if not _EVAL_CONFIDENCE_DATASET_PATH.exists():
        return {}
    raw = json.loads(_EVAL_CONFIDENCE_DATASET_PATH.read_text(encoding="utf-8"))
    by_id = {q["id"]: q for q in raw.get("questions", [])}
    return {qid: by_id[qid] for qid in ids if qid in by_id}


def load_repository_derived_annotatable_cases() -> list[AnswerQualityCase]:
    """Three `provenance="repository_derived"` cases built entirely from
    `scripts/eval_confidence_dataset.json`'s OWN already-committed
    `evidence`/`expected_answer` fields -- no live retrieval, no database.
    Deliberately reuses that file's own prior curation (evidence a person
    already decided was the right supporting set for each question) rather
    than inventing new "realistic-looking" content, and sidesteps section
    12's evidence-drift concern structurally: this evidence cannot drift,
    because it is never fetched live in the first place.

    One case per category this priority's answer-mode taxonomy already
    maps (`_CATEGORY_TO_EXPECTED_MODE`): `sso-login-failure` (clear-answer
    -> `"answer"`, using the dataset's own `expected_answer` as
    `fixed_answer`), `python-version-conflict` (ambiguous ->
    `"qualified_answer"`, same mechanism), `neg-parental-leave`
    (no-information -> `"no_answer"`, using the production
    `_INSUFFICIENT_GROUNDING_MESSAGE` sentinel as `fixed_answer` since that
    question has no evidence to hedge about at all).

    Returns `[]` (not an error) if the dataset file doesn't exist -- same
    honest-empty convention as every other loader in this module.
    """
    from app.agents.answer.node import _INSUFFICIENT_GROUNDING_MESSAGE

    questions = _load_dataset_questions_by_id(
        "sso-login-failure", "python-version-conflict", "neg-parental-leave"
    )
    cases: list[AnswerQualityCase] = []
    if "sso-login-failure" in questions:
        q = questions["sso-login-failure"]
        cases.append(
            AnswerQualityCase(
                id=f"annot-repo-{q['id']}",
                provenance="repository_derived",
                question=q["question"],
                evidence_texts=q.get("evidence", []),
                reference_answer=q.get("expected_answer"),
                expected_answer_mode="answer",
                fixed_answer=q["expected_answer"],
                tags=["annotatable", "repository-derived", q["category"]],
            )
        )
    if "python-version-conflict" in questions:
        q = questions["python-version-conflict"]
        cases.append(
            AnswerQualityCase(
                id=f"annot-repo-{q['id']}",
                provenance="repository_derived",
                question=q["question"],
                evidence_texts=q.get("evidence", []),
                reference_answer=q.get("expected_answer"),
                expected_answer_mode="qualified_answer",
                fixed_answer=q["expected_answer"],
                tags=["annotatable", "repository-derived", q["category"]],
            )
        )
    if "neg-parental-leave" in questions:
        q = questions["neg-parental-leave"]
        cases.append(
            AnswerQualityCase(
                id=f"annot-repo-{q['id']}",
                provenance="repository_derived",
                question=q["question"],
                evidence_texts=q.get("evidence", []),
                reference_answer=None,
                expected_answer_mode="no_answer",
                fixed_answer=_INSUFFICIENT_GROUNDING_MESSAGE,
                tags=["annotatable", "repository-derived", q["category"]],
            )
        )
    return cases


def load_annotatable_answer_quality_cases() -> list[AnswerQualityCase]:
    """The full human-annotatable answer-quality corpus: the 6 contrast
    cases (Priority 9 -- already `fixed_answer`, already span correct
    answer/hallucination/correct refusal/incorrect refusal/qualified/
    overconfident) plus the 3 repository-derived cases above. Every case
    returned here has `fixed_answer` set -- `scripts/
    annotate_semantic_cases.py` refuses to accept an annotation for any
    case without one (see that script's own validation).
    """
    return list(CONTRAST_ANSWER_QUALITY_CASES) + load_repository_derived_annotatable_cases()
