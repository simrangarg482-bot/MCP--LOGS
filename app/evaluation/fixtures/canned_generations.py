"""Canned `(answer, citations, confidence)` / `(evidence, hypotheses)`
outputs for `FixtureAnswerAdapter`/`FixtureInvestigationAdapter`, keyed by
case id. See `app.evaluation.fixtures`'s package docstring and
`app.evaluation.adapters.generation`'s module docstring for why these are
authored here rather than read from the JSONL dataset files themselves.

Deliberately includes both cases that pass and cases engineered to fail each
distinct check this package's metrics can detect (unresolved citation,
unsupported/fabricated citation excerpt, citation-count shortfall, an
unmatched hypothesis, insufficient hypothesis support, and a hypothesis
citing evidence that was never gathered) -- a fixture suite that only ever
passes would not actually prove the evaluation logic detects anything.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.evaluation.fixtures import stable_uuid
from app.shared.schemas.agent_contracts import Citation, EvidenceItem, RootCauseHypothesis

_NOW = datetime(2026, 8, 1, 15, 0, tzinfo=UTC)


def _citation(label: str, excerpt: str) -> Citation:
    return Citation(
        document_id=stable_uuid(label),
        chunk_id=stable_uuid(f"{label}:chunk"),
        source_url=f"https://example.invalid/{label}",
        excerpt=excerpt,
    )


# --------------------------------------------------------------------------
# answer category
# --------------------------------------------------------------------------

CANNED_ANSWERS: dict[str, tuple[str, list[Citation], float]] = {
    "answer-clear-001": (
        "The authentication service failed because deployment 456 reduced the "
        "database connection pool size from 100 to 10, exhausting the pool "
        "under normal load.",
        [
            _citation("incident-123", "the database connection pool was exhausted"),
            _citation("deploy-456", "DB_POOL_SIZE was reduced from 100 to 10"),
        ],
        0.82,
    ),
    "answer-contains-002": (
        "Restore DB_POOL_SIZE to a safe value and redeploy the authentication service.",
        [_citation("runbook-auth-001", "Restore DB_POOL_SIZE to a safe value and redeploy")],
        0.70,
    ),
    "answer-regex-003": (
        "The connection pool size was reduced from 100 to 10 connections.",
        [_citation("deploy-456", "DB_POOL_SIZE was reduced from 100 to 10")],
        0.75,
    ),
    "answer-containsany-004": (
        "The most likely cause is database connection pool exhaustion following "
        "the recent deployment.",
        [_citation("incident-123", "the database connection pool was exhausted")],
        0.65,
    ),
    # Deliberately fails its `exact_match` assertion (a real paraphrase, not
    # the exact expected sentence) -- proves the harness reports a genuine
    # generation-stage failure rather than trivially passing everything.
    "answer-exactmatch-005": (
        "Database connection pool exhaustion caused by deployment 456's configuration change.",
        [_citation("incident-123", "the database connection pool was exhausted")],
        0.78,
    ),
}


# --------------------------------------------------------------------------
# grounding category (only cases that reach the answer-generation step --
# `grounding-untraceable-002`/`grounding-forbidden-003` fail at the
# retrieval stage first and never call the answer adapter)
# --------------------------------------------------------------------------

CANNED_ANSWERS.update(
    {
        "grounding-traceable-001": (
            "The authentication service failed because deployment 456 reduced "
            "the database connection pool size from 100 to 10.",
            [
                _citation("incident-123", "the database connection pool was exhausted"),
                _citation("deploy-456", "DB_POOL_SIZE was reduced from 100 to 10"),
            ],
            0.80,
        ),
        # Fabricated excerpt: not a real substring of incident-123's actual
        # content -- `check_citations` must flag this as unsupported.
        "grounding-unsupported-citation-004": (
            "The authentication service failed due to connection pool exhaustion, "
            "confirmed by the incident report.",
            [_citation("incident-123", "the pool was completely full and unrecoverable")],
            0.60,
        ),
        # Cites a document that was never retrieved -- must be flagged as
        # unresolved, not unsupported (a different failure mode).
        "grounding-unresolved-citation-005": (
            "The authentication service failed due to connection pool exhaustion.",
            [_citation("nonexistent-doc", "something")],
            0.60,
        ),
        # Only 1 citation when the case expects a minimum of 2.
        "grounding-count-006": (
            "The authentication service failed due to connection pool exhaustion.",
            [_citation("incident-123", "the database connection pool was exhausted")],
            0.70,
        ),
    }
)


# --------------------------------------------------------------------------
# investigation category
# --------------------------------------------------------------------------


def _evidence(source: str, reference: str, summary: str) -> EvidenceItem:
    return EvidenceItem(source=source, reference=reference, summary=summary, retrieved_at=_NOW)  # type: ignore[arg-type]


_CLEAR_EVIDENCE = [
    _evidence("deployment", "deploy-456", "Deployment 456 reduced DB_POOL_SIZE from 100 to 10."),
    _evidence(
        "postmortem",
        "incident-123",
        "Auth service outage root-caused to connection pool exhaustion.",
    ),
]
_CLEAR_HYPOTHESIS = RootCauseHypothesis(
    description=(
        "The deployment reduced the database connection pool size, causing pool "
        "exhaustion under load."
    ),
    confidence=0.80,
    supporting_evidence_ids=["deploy-456", "incident-123"],
)

CANNED_INVESTIGATIONS: dict[str, tuple[list[EvidenceItem], list[RootCauseHypothesis]]] = {
    "investigation-clear-001": (_CLEAR_EVIDENCE, [_CLEAR_HYPOTHESIS]),
    # Same evidence/hypothesis as the clear case, but the dataset's own
    # `required_evidence_ids` will ask for one more reference than was ever
    # gathered -- a retrieval-stage failure (evidence_coverage < 1.0).
    "investigation-missing-evidence-002": (_CLEAR_EVIDENCE, [_CLEAR_HYPOTHESIS]),
    # The dataset's expected hypothesis concept ("rollback") is never
    # mentioned by either produced hypothesis -- a generation-stage failure
    # (no hypothesis matched at all).
    "investigation-unmatched-hypothesis-003": (_CLEAR_EVIDENCE, [_CLEAR_HYPOTHESIS]),
    # Matches on concept, but the dataset will require `minimum_support=2`
    # while this hypothesis only overlaps on one required evidence id.
    "investigation-insufficient-support-004": (
        [
            _evidence(
                "deployment", "deploy-456", "Deployment 456 reduced DB_POOL_SIZE from 100 to 10."
            )
        ],
        [
            RootCauseHypothesis(
                description="The deployment change to the connection pool size caused the outage.",
                confidence=0.55,
                supporting_evidence_ids=["deploy-456"],
            )
        ],
    ),
    # Cites support that was never gathered at all -- a hallucinated
    # reference, flagged by `find_unsupported_hypotheses` regardless of
    # what the dataset's own expected hypotheses ask for.
    "investigation-unsupported-hallucination-005": (
        [
            _evidence(
                "deployment", "deploy-456", "Deployment 456 reduced DB_POOL_SIZE from 100 to 10."
            )
        ],
        [
            RootCauseHypothesis(
                description=(
                    "The connection pool change caused the outage, per a source "
                    "that was never actually gathered."
                ),
                confidence=0.40,
                supporting_evidence_ids=["fabricated-ref-999"],
            )
        ],
    ),
    # The four cases below only exercise the critique check
    # (`ExpectedCritique`) -- their evidence/hypotheses are the same
    # well-formed shape as `investigation-clear-001`'s, since what varies
    # per case is the CANNED CRITIQUE OUTCOME below, not this input.
    "investigation-critique-accept-006": (_CLEAR_EVIDENCE, [_CLEAR_HYPOTHESIS]),
    "investigation-critique-reject-007": (_CLEAR_EVIDENCE, [_CLEAR_HYPOTHESIS]),
    "investigation-critique-review-failed-008": (_CLEAR_EVIDENCE, [_CLEAR_HYPOTHESIS]),
    "investigation-critique-revise-then-accept-009": (_CLEAR_EVIDENCE, [_CLEAR_HYPOTHESIS]),
    "investigation-critique-regression-negative-control-010": (
        _CLEAR_EVIDENCE,
        [_CLEAR_HYPOTHESIS],
    ),
}


# --------------------------------------------------------------------------
# investigation critique (Priority 7) -- (review_status, critique_verdict,
# revision_count), the same three fields `agents.investigation.critique.
# ReviewOutcome`/`InvestigationResult` carry. These are canned OUTCOMES
# standing in for what a real bounded critique pass would conclude given
# `CANNED_INVESTIGATIONS`'s evidence/hypotheses above -- not re-derived by
# running `agents.investigation.critique` itself, the same "fixture, not
# a live call" convention every canned output in this module follows.
# --------------------------------------------------------------------------

CANNED_CRITIQUES: dict[str, tuple[str, str | None, int]] = {
    # Well-supported hypothesis, two real citations -- critique accepts
    # without needing a revision.
    "investigation-critique-accept-006": ("reviewed", "accept", 0),
    # Hallucinated citation reused from investigation-unsupported-
    # hallucination-005's evidence/hypothesis shape -- critique rejects it.
    "investigation-critique-reject-007": ("reviewed", "reject", 0),
    # The critique model itself was unavailable -- the original hypothesis
    # is preserved, but must never be reported as "reviewed".
    "investigation-critique-review-failed-008": ("review_failed", None, 0),
    # One bounded revision resolved the critique's concern.
    "investigation-critique-revise-then-accept-009": ("reviewed", "accept", 1),
    # Same outcome as the accept case above; the DATASET case deliberately
    # expects "reject" instead -- proves the harness detects a real
    # critique-outcome mismatch as a regression, not just a lucky match.
    "investigation-critique-regression-negative-control-010": ("reviewed", "accept", 0),
}
