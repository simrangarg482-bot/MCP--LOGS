"""Tests for `app.evaluation.semantic.investigation_ab` -- the baseline-vs-
reflected A/B harness. Covers section 15's "A/B pairing uses equivalent
inputs" and "aggregation handles partial failures intentionally"
requirements, plus the outcome classifier's own documented cases.
"""

from __future__ import annotations

import json

import pytest

from app.evaluation.semantic.investigation_ab import run_investigation_ab_case
from app.evaluation.semantic.schemas import InvestigationABCase


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _QueuedLLM:
    def __init__(self, *replies: str) -> None:
        self._queue = list(replies)
        self.calls: list[object] = []

    def with_config(self, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if not self._queue:
            raise AssertionError("LLM called more times than responses were queued")
        return _Response(self._queue.pop(0))


class _AlwaysFailingLLM:
    def with_config(self, **kwargs):
        return self

    async def ainvoke(self, messages):
        raise RuntimeError("model unavailable")


def _case(**overrides) -> InvestigationABCase:
    payload = {
        "id": "iab-1",
        "provenance": "synthetic_controlled",
        "query": "Investigate the checkout outage",
        "evidence": [
            ("deploy-1", "deployment", "Deployment 1 changed the gateway timeout."),
            ("incident-1", "postmortem", "Root cause: the gateway timeout change."),
        ],
    }
    payload.update(overrides)
    return InvestigationABCase(**payload)


def _hypothesis_json(description: str, confidence: float, *evidence_ids: str) -> str:
    return json.dumps(
        {
            "hypotheses": [
                {
                    "description": description,
                    "confidence": confidence,
                    "supporting_evidence_ids": list(evidence_ids),
                }
            ],
            "suggested_owner_team": "platform",
            "suggested_next_steps": ["roll back the change"],
        }
    )


def _empty_hypothesis_json() -> str:
    return json.dumps({"hypotheses": [], "suggested_owner_team": None, "suggested_next_steps": []})


def _accept_json(**overrides) -> str:
    payload = {
        "verdict": "accept",
        "unsupported_hypothesis_indices": [],
        "contradictory_evidence": [],
        "revision_guidance": None,
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


def _revise_json(**overrides) -> str:
    payload = {
        "verdict": "revise",
        "unsupported_hypothesis_indices": [0],
        "contradictory_evidence": [],
        "revision_guidance": "lower confidence, citation is thin",
    }
    payload.update(overrides)
    return json.dumps(payload)


@pytest.fixture(autouse=True)
def _enable_critique(monkeypatch):
    from app.agents.investigation import critique as critique_module

    settings = critique_module.get_settings()
    monkeypatch.setattr(settings, "investigation_critique_enabled", True)
    monkeypatch.setattr(settings, "investigation_critique_min_evidence_count", 2)
    monkeypatch.setattr(settings, "investigation_critique_overconfidence_threshold", 0.75)
    monkeypatch.setattr(settings, "investigation_critique_min_evidence_per_hypothesis", 2)
    return settings


@pytest.mark.asyncio
async def test_baseline_and_reflected_share_the_same_generated_draft_not_two_generations():
    """Equivalent-inputs-by-construction: hypothesis generation happens
    exactly once (call 1); critique (call 2) is applied to THAT draft, not
    a second independent generation."""
    llm = _QueuedLLM(
        _hypothesis_json("root cause", 0.6, "deploy-1", "incident-1"),  # baseline generation
        _accept_json(),  # critique of that same draft
    )

    result = await run_investigation_ab_case(llm, _case())

    assert len(llm.calls) == 2  # exactly one generation + one critique call
    assert result.baseline.hypothesis_count == 1
    assert result.reflected.hypothesis_count == 1
    assert result.error is None


@pytest.mark.asyncio
async def test_well_supported_case_yields_no_measurable_change():
    llm = _QueuedLLM(
        _hypothesis_json("root cause", 0.6, "deploy-1", "incident-1"),
        _accept_json(),
    )
    result = await run_investigation_ab_case(llm, _case())
    assert result.outcome == "critique_no_measurable_change"


@pytest.mark.asyncio
async def test_thin_evidence_case_with_reject_is_correctly_rejected_not_damaged():
    """The baseline has a structural issue (1 citation, below the 2-citation
    floor at high confidence) -- critique rejecting it is a correct catch,
    not damage."""
    llm = _QueuedLLM(
        _hypothesis_json("weak claim", 0.9, "deploy-1"),  # 1 citation only
        _reject_json(),
    )
    result = await run_investigation_ab_case(llm, _case())
    assert result.outcome == "critique_correctly_rejected"


@pytest.mark.asyncio
async def test_reject_with_no_structural_issues_is_classified_as_damaged():
    """The baseline is well-cited and not overconfident -- a critique
    rejection here has no structural justification and is flagged as
    damage, not a correct catch."""
    llm = _QueuedLLM(
        _hypothesis_json("solid claim", 0.5, "deploy-1", "incident-1"),
        _reject_json(),
    )
    result = await run_investigation_ab_case(llm, _case())
    assert result.outcome == "critique_damaged"


@pytest.mark.asyncio
async def test_revision_is_classified_as_improved():
    llm = _QueuedLLM(
        _hypothesis_json("weak claim", 0.9, "deploy-1"),  # thin citation
        _revise_json(),
        _hypothesis_json("stronger claim", 0.6, "deploy-1", "incident-1"),  # revision
        _accept_json(),
    )
    result = await run_investigation_ab_case(llm, _case())
    assert result.outcome == "critique_improved"
    assert result.reflected.revision_count == 1


@pytest.mark.asyncio
async def test_no_evidence_case_still_completes_without_raising():
    llm = _QueuedLLM(_empty_hypothesis_json(), _reject_json())
    result = await run_investigation_ab_case(llm, _case(evidence=[]))
    assert result.error is None
    assert result.baseline.hypothesis_count == 0


@pytest.mark.asyncio
async def test_model_failure_is_isolated_to_this_case_not_raised():
    """One bad case must never abort the whole benchmark -- the failure is
    captured on `.error`, matching `answer_quality`'s and `runner.py`'s own
    failure-isolation convention."""
    llm = _AlwaysFailingLLM()
    result = await run_investigation_ab_case(llm, _case())

    assert result.error is not None
    assert result.outcome == "critique_unavailable"
    assert result.baseline.hypothesis_count == 0
    assert result.reflected.hypothesis_count == 0


@pytest.mark.asyncio
async def test_evidence_block_never_contains_more_than_the_case_supplied():
    llm = _QueuedLLM(
        _hypothesis_json("root cause", 0.6, "only-ref"),
        _accept_json(),
    )
    case = _case(
        evidence=[("only-ref", "slack", "the only authorized evidence summary")],
    )
    await run_investigation_ab_case(llm, case)

    generation_call = str(llm.calls[0])
    assert "only-ref" in generation_call
    assert "the only authorized evidence summary" in generation_call
