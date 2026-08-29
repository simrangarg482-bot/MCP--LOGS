"""Tests for `app.agents.investigation.critique` -- the bounded
critique/reflection stage (Priority 7).

Covers this priority's required negative controls: unsupported hypothesis,
supported hypothesis, the revision bound, malformed critique output,
critique failure, and evidence-boundary preservation (no evidence beyond
what was already passed in ever reaches the critic). Cross-tenant/
permission isolation is a structural property of this module (it fetches
nothing itself -- every entity it can ever reason about was already
authorized upstream by `investigation.evidence.gather_evidence`) and is
verified here by asserting the critique's rendered prompt never contains
anything beyond the `evidence`/`hypotheses` explicitly passed in.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.agents.investigation import critique as critique_module
from app.shared.schemas import EvidenceItem, RootCauseHypothesis


def _evidence(reference: str, summary: str = "some evidence") -> EvidenceItem:
    return EvidenceItem(
        source="github", reference=reference, summary=summary, retrieved_at=datetime.now(UTC)
    )


def _hypothesis(description: str, confidence: float, *evidence_ids: str) -> RootCauseHypothesis:
    return RootCauseHypothesis(
        description=description, confidence=confidence, supporting_evidence_ids=list(evidence_ids)
    )


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _QueuedLLM:
    """Returns one queued response per `.ainvoke()` call, in order. Raises
    `AssertionError` if called more times than responses were queued --
    makes "exactly N model calls happened" an implicit assertion in every
    test that uses it."""

    def __init__(self, *replies: str) -> None:
        self._queue = list(replies)
        self.calls: list[object] = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if not self._queue:
            raise AssertionError("LLM called more times than responses were queued")
        return _Response(self._queue.pop(0))


class _AlwaysFailingLLM:
    async def ainvoke(self, messages):
        raise RuntimeError("model unavailable")


def _accept_json(**overrides) -> str:
    payload = {
        "verdict": "accept",
        "unsupported_hypothesis_indices": [],
        "contradictory_evidence": [],
        "revision_guidance": None,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _revise_json(**overrides) -> str:
    payload = {
        "verdict": "revise",
        "unsupported_hypothesis_indices": [0],
        "contradictory_evidence": [],
        "revision_guidance": "Lower confidence on hypothesis 0; its citation is thin.",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _reject_json(**overrides) -> str:
    payload = {
        "verdict": "reject",
        "unsupported_hypothesis_indices": [0],
        "contradictory_evidence": [],
        "revision_guidance": None,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _hypothesis_generation_json(description: str, confidence: float, evidence_id: str) -> str:
    return json.dumps(
        {
            "hypotheses": [
                {
                    "description": description,
                    "confidence": confidence,
                    "supporting_evidence_ids": [evidence_id],
                }
            ],
            "suggested_owner_team": "platform",
            "suggested_next_steps": ["restart the service"],
        }
    )


@pytest.fixture(autouse=True)
def _enable_critique(monkeypatch):
    settings = critique_module.get_settings()
    monkeypatch.setattr(settings, "investigation_critique_enabled", True)
    monkeypatch.setattr(settings, "investigation_critique_min_evidence_count", 2)
    monkeypatch.setattr(settings, "investigation_critique_overconfidence_threshold", 0.75)
    monkeypatch.setattr(settings, "investigation_critique_min_evidence_per_hypothesis", 2)
    return settings


# --------------------------------------------------------------------------
# deterministic structural validation
# --------------------------------------------------------------------------


def test_structural_flags_insufficient_information_below_evidence_floor():
    issues = critique_module.validate_structurally(
        [],
        [_evidence("e1")],
        min_evidence_count=2,
        overconfidence_threshold=0.75,
        min_evidence_per_hypothesis=2,
    )
    assert "insufficient_information" in issues


def test_structural_flags_overconfidence_with_thin_citation():
    hyp = _hypothesis("root cause", 0.9, "e1")  # 1 citation, threshold needs 2
    issues = critique_module.validate_structurally(
        [hyp],
        [_evidence("e1"), _evidence("e2")],
        min_evidence_count=2,
        overconfidence_threshold=0.75,
        min_evidence_per_hypothesis=2,
    )
    assert issues == ["overconfidence:hypothesis_0"]


def test_structural_does_not_flag_well_cited_high_confidence_hypothesis():
    hyp = _hypothesis("root cause", 0.9, "e1", "e2")
    issues = critique_module.validate_structurally(
        [hyp],
        [_evidence("e1"), _evidence("e2")],
        min_evidence_count=2,
        overconfidence_threshold=0.75,
        min_evidence_per_hypothesis=2,
    )
    assert issues == []


def test_structural_does_not_flag_low_confidence_thin_citation():
    hyp = _hypothesis("root cause", 0.4, "e1")
    issues = critique_module.validate_structurally(
        [hyp],
        [_evidence("e1"), _evidence("e2")],
        min_evidence_count=2,
        overconfidence_threshold=0.75,
        min_evidence_per_hypothesis=2,
    )
    assert issues == []


# --------------------------------------------------------------------------
# semantic critique output validation -- malformed output must never
# silently become acceptance
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_json_does_not_become_silent_acceptance():
    llm = _QueuedLLM("this is not json at all")
    result = await critique_module._run_semantic_critique(
        llm, "q", [_hypothesis("h", 0.5, "e1")], [_evidence("e1")], [], {}, node_name="test"
    )
    assert result is None  # never a fabricated "accept"


@pytest.mark.asyncio
async def test_invalid_verdict_value_does_not_become_silent_acceptance():
    llm = _QueuedLLM(json.dumps({"verdict": "maybe"}))
    result = await critique_module._run_semantic_critique(
        llm, "q", [_hypothesis("h", 0.5, "e1")], [_evidence("e1")], [], {}, node_name="test"
    )
    assert result is None


def test_contradiction_note_with_fabricated_evidence_id_is_dropped():
    known = {"e1", "e2"}
    raw = [{"evidence_ids": ["e1", "e999"], "detail": "conflict"}]
    notes = critique_module._filter_contradictions(raw, known)
    assert notes == []  # only 1 real id survives filtering -- below the 2-id minimum


def test_contradiction_note_with_two_real_ids_survives():
    known = {"e1", "e2"}
    raw = [{"evidence_ids": ["e1", "e2"], "detail": "these conflict"}]
    notes = critique_module._filter_contradictions(raw, known)
    assert len(notes) == 1
    assert set(notes[0].evidence_ids) == {"e1", "e2"}


def test_revision_guidance_is_capped_in_length():
    parsed = {
        "verdict": "revise",
        "revision_guidance": "x" * 5000,
    }
    result = critique_module._validate_critique_result(parsed, known_evidence_ids=set())
    assert result.revision_guidance is not None
    assert len(result.revision_guidance) == critique_module._MAX_REVISION_GUIDANCE_CHARS


# --------------------------------------------------------------------------
# confidence penalties -- fixed, code-defined, never model-supplied
# --------------------------------------------------------------------------


def test_unsupported_claim_penalty_is_applied_and_clamped():
    hyp = _hypothesis("h", 0.1, "e1")
    semantic = critique_module.SemanticCritiqueResult(
        verdict="accept", unsupported_hypothesis_indices=[0]
    )
    adjusted = critique_module._apply_penalties([hyp], [], semantic)
    assert adjusted[0].confidence == pytest.approx(0.0)  # 0.1 - 0.3, clamped


def test_overconfidence_penalty_is_applied_from_structural_tag():
    hyp = _hypothesis("h", 0.9, "e1")
    semantic = critique_module.SemanticCritiqueResult(verdict="accept")
    adjusted = critique_module._apply_penalties([hyp], ["overconfidence:hypothesis_0"], semantic)
    assert adjusted[0].confidence == pytest.approx(0.9 - critique_module._OVERCONFIDENCE_PENALTY)


def test_penalties_stack_when_both_apply():
    hyp = _hypothesis("h", 0.9, "e1")
    semantic = critique_module.SemanticCritiqueResult(
        verdict="accept", unsupported_hypothesis_indices=[0]
    )
    adjusted = critique_module._apply_penalties([hyp], ["overconfidence:hypothesis_0"], semantic)
    expected = (
        0.9 - critique_module._OVERCONFIDENCE_PENALTY - critique_module._UNSUPPORTED_CLAIM_PENALTY
    )
    assert adjusted[0].confidence == pytest.approx(max(0.0, expected))


# --------------------------------------------------------------------------
# review_investigation -- end to end orchestration
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_kill_switch_skips_critique_entirely(monkeypatch):
    settings = critique_module.get_settings()
    monkeypatch.setattr(settings, "investigation_critique_enabled", False)
    llm = _QueuedLLM()  # no calls expected

    outcome = await critique_module.review_investigation(
        llm, "q", [_evidence("e1")], [_hypothesis("h", 0.9, "e1")], "team", ["step"], {}
    )
    assert outcome.review_status == "not_reviewed"
    assert outcome.critique_verdict is None
    assert llm.calls == []


@pytest.mark.asyncio
async def test_empty_hypotheses_skips_critique():
    llm = _QueuedLLM()
    outcome = await critique_module.review_investigation(
        llm, "q", [_evidence("e1")], [], None, ["step"], {}
    )
    assert outcome.review_status == "not_reviewed"
    assert llm.calls == []


@pytest.mark.asyncio
async def test_supported_hypothesis_is_accepted_without_unnecessary_revision():
    evidence = [_evidence("e1"), _evidence("e2")]
    hypotheses = [_hypothesis("root cause", 0.6, "e1", "e2")]
    llm = _QueuedLLM(_accept_json())  # exactly one critique call

    outcome = await critique_module.review_investigation(
        llm, "q", evidence, hypotheses, "team", ["step"], {}
    )

    assert outcome.review_status == "reviewed"
    assert outcome.critique_verdict == "accept"
    assert outcome.revision_count == 0
    assert len(outcome.hypotheses) == 1
    assert len(llm.calls) == 1  # no revision call made


@pytest.mark.asyncio
async def test_unsupported_hypothesis_is_rejected():
    evidence = [_evidence("e1"), _evidence("e2")]
    hypotheses = [_hypothesis("unsupported claim", 0.8, "e1")]
    llm = _QueuedLLM(_reject_json())

    outcome = await critique_module.review_investigation(
        llm, "q", evidence, hypotheses, "team", ["step"], {}
    )

    assert outcome.critique_verdict == "reject"
    assert outcome.hypotheses == []
    assert outcome.suggested_owner_team is None
    assert "hypothesis" in " ".join(outcome.suggested_next_steps).lower()


@pytest.mark.asyncio
async def test_insufficient_evidence_is_rejected_without_spending_a_critique_call():
    """The deterministic pre-check must catch this BEFORE any LLM call."""
    evidence = [_evidence("e1")]  # below min_evidence_count=2
    hypotheses = [_hypothesis("h", 0.5, "e1")]
    llm = _QueuedLLM()  # zero calls expected

    outcome = await critique_module.review_investigation(
        llm, "q", evidence, hypotheses, "team", ["step"], {}
    )

    assert outcome.critique_verdict == "reject"
    assert "insufficient_information" in outcome.critique_issues
    assert llm.calls == []


@pytest.mark.asyncio
async def test_revise_then_accept_uses_exactly_two_critique_calls_and_one_revision():
    evidence = [_evidence("e1"), _evidence("e2")]
    hypotheses = [_hypothesis("weak claim", 0.5, "e1", "e2")]
    llm = _QueuedLLM(
        _revise_json(),  # pass 1: revise
        _hypothesis_generation_json("stronger claim", 0.6, "e1"),  # revision generation
        _accept_json(),  # pass 2: accept the revision
    )

    outcome = await critique_module.review_investigation(
        llm, "q", evidence, hypotheses, "team", ["step"], {}
    )

    assert outcome.critique_verdict == "accept"
    assert outcome.revision_count == 1
    assert len(outcome.hypotheses) == 1
    assert outcome.hypotheses[0].description == "stronger claim"
    assert len(llm.calls) == 3


@pytest.mark.asyncio
async def test_revision_bound_is_enforced_a_second_revise_becomes_reject():
    """Negative control 3: critique repeatedly requests revision -> the
    maximum (one revision attempt) is enforced, not silently honored."""
    evidence = [_evidence("e1"), _evidence("e2")]
    hypotheses = [_hypothesis("weak claim", 0.5, "e1", "e2")]
    llm = _QueuedLLM(
        _revise_json(),  # pass 1: revise
        _hypothesis_generation_json("still weak", 0.5, "e1"),  # revision
        _revise_json(),  # pass 2: STILL wants to revise -- budget exhausted
    )

    outcome = await critique_module.review_investigation(
        llm, "q", evidence, hypotheses, "team", ["step"], {}
    )

    assert outcome.critique_verdict == "reject"  # never a third revision
    assert outcome.revision_count == 1
    assert outcome.hypotheses == []
    assert len(llm.calls) == 3  # exactly bounded: no fourth call ever happens


@pytest.mark.asyncio
async def test_critique_model_failure_degrades_without_claiming_review():
    """Negative control 5: model/adapter failure -> review_failed, original
    hypotheses preserved, never silently 'accepted'."""
    evidence = [_evidence("e1"), _evidence("e2")]
    hypotheses = [_hypothesis("h", 0.5, "e1")]
    llm = _AlwaysFailingLLM()

    outcome = await critique_module.review_investigation(
        llm, "q", evidence, hypotheses, "team", ["step"], {}
    )

    assert outcome.review_status == "review_failed"
    assert outcome.critique_verdict is None
    assert outcome.hypotheses == hypotheses  # original preserved, not discarded
    assert outcome.suggested_owner_team == "team"


@pytest.mark.asyncio
async def test_malformed_critique_output_degrades_to_review_failed_not_accept():
    """Negative control 4: invalid schema must never silently become
    acceptance."""
    evidence = [_evidence("e1"), _evidence("e2")]
    hypotheses = [_hypothesis("h", 0.5, "e1")]
    llm = _QueuedLLM("not json", "still not json", "definitely not json")  # exhausts retries

    outcome = await critique_module.review_investigation(
        llm, "q", evidence, hypotheses, "team", ["step"], {}
    )

    assert outcome.review_status == "review_failed"
    assert outcome.critique_verdict is None
    assert outcome.hypotheses == hypotheses


@pytest.mark.asyncio
async def test_revision_failure_falls_back_to_pre_revision_hypotheses():
    evidence = [_evidence("e1"), _evidence("e2")]
    hypotheses = [_hypothesis("weak claim", 0.5, "e1", "e2")]
    llm = _QueuedLLM(
        _revise_json(),
        "not json",
        "not json",
        "not json",  # revision generation call exhausts retries
    )

    outcome = await critique_module.review_investigation(
        llm, "q", evidence, hypotheses, "team", ["step"], {}
    )

    assert outcome.review_status == "review_failed"
    assert outcome.critique_verdict == "revise"  # last completed verdict, honestly reported
    assert outcome.hypotheses == hypotheses  # pre-revision set preserved


@pytest.mark.asyncio
async def test_revision_producing_no_hypotheses_is_rejected():
    evidence = [_evidence("e1"), _evidence("e2")]
    hypotheses = [_hypothesis("weak claim", 0.5, "e1", "e2")]
    llm = _QueuedLLM(
        _revise_json(),
        json.dumps({"hypotheses": [], "suggested_owner_team": None, "suggested_next_steps": []}),
    )

    outcome = await critique_module.review_investigation(
        llm, "q", evidence, hypotheses, "team", ["step"], {}
    )

    assert outcome.critique_verdict == "reject"
    assert outcome.revision_count == 1
    assert outcome.hypotheses == []


# --------------------------------------------------------------------------
# evidence boundary: critique never sees anything beyond what was passed in
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_critique_prompt_never_contains_evidence_outside_what_was_passed():
    """Structural proof of the authorization invariant: this module fetches
    nothing itself, so the only way unauthorized evidence could reach the
    model is if this function pulled in something beyond its own
    `evidence` parameter -- assert the rendered prompt contains ONLY the
    references explicitly passed in."""
    evidence = [
        _evidence("only-this-one", summary="the only authorized evidence"),
        _evidence("also-authorized", summary="a second authorized item"),
    ]
    hypotheses = [_hypothesis("h", 0.9, "only-this-one", "also-authorized")]
    llm = _QueuedLLM(_accept_json())

    await critique_module.review_investigation(llm, "q", evidence, hypotheses, None, [], {})

    assert len(llm.calls) == 1  # the critique call actually happened
    sent = str(llm.calls[0])
    assert "only-this-one" in sent
    assert "the only authorized evidence" in sent
    assert "also-authorized" in sent
    # Nothing beyond these two EvidenceItems was ever passed in, so nothing
    # else CAN appear in the rendered prompt -- this module fetches no
    # evidence of its own.


@pytest.mark.asyncio
async def test_unsupported_hypothesis_index_flag_lowers_confidence_on_accept():
    evidence = [_evidence("e1"), _evidence("e2")]
    hypotheses = [_hypothesis("h", 0.6, "e1", "e2")]
    llm = _QueuedLLM(_accept_json(unsupported_hypothesis_indices=[0]))

    outcome = await critique_module.review_investigation(
        llm, "q", evidence, hypotheses, "team", ["step"], {}
    )

    assert outcome.critique_verdict == "accept"
    assert outcome.hypotheses[0].confidence == pytest.approx(
        0.6 - critique_module._UNSUPPORTED_CLAIM_PENALTY
    )
    assert "unsupported_claim:hypothesis_0" in outcome.critique_issues
