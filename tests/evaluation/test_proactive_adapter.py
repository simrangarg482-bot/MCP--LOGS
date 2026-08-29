"""Tests for `app.evaluation.adapters.proactive` -- the permission-aware,
mixed-visibility finding resolution seam the "proactive" evaluation
category runs against.

Same shape as `test_graph_adapter.py`'s permission-aware filtering
requirement: the same fixture corpus, different identity permissions must
produce a different visible set.
"""

from __future__ import annotations

import pytest

from app.evaluation.adapters.proactive import FixtureProactiveAdapter
from app.evaluation.fixtures.canned_generations import CANNED_ANSWERS, CANNED_INVESTIGATIONS
from app.evaluation.fixtures.corpus import CORPUS
from app.evaluation.fixtures.proactive_corpus import EVIDENCE, FINDINGS
from app.evaluation.runner import build_deterministic_runner
from app.evaluation.schemas import EvalIdentity, EvaluationCase


def _case(**overrides) -> EvaluationCase:
    defaults = dict(id="t", category="proactive", query="proactive findings")
    defaults.update(overrides)
    return EvaluationCase(**defaults)


@pytest.mark.asyncio
async def test_no_permissions_sees_nothing():
    adapter = FixtureProactiveAdapter(FINDINGS, EVIDENCE)
    case = _case(identity=EvalIdentity(permissions=frozenset()))
    assert await adapter.list_findings(case) == []


@pytest.mark.asyncio
async def test_incident_read_reveals_the_clean_recurring_severity_finding():
    adapter = FixtureProactiveAdapter(FINDINGS, EVIDENCE)
    case = _case(identity=EvalIdentity(permissions=frozenset({"incident:read"})))
    visible = await adapter.list_findings(case)
    assert "recurring-severity-platform" in visible


@pytest.mark.asyncio
async def test_multi_document_finding_is_hidden_below_threshold_without_review_permission():
    adapter = FixtureProactiveAdapter(FINDINGS, EVIDENCE)
    reader_only = _case(identity=EvalIdentity(permissions=frozenset({"incident:read"})))
    reviewer = _case(
        identity=EvalIdentity(permissions=frozenset({"incident:read", "knowledge:review"}))
    )

    without_review = await adapter.list_findings(reader_only)
    with_review = await adapter.list_findings(reviewer)

    assert "multi-document-checkout-incident" not in without_review
    assert "multi-document-checkout-incident" in with_review


@pytest.mark.asyncio
async def test_deleted_evidence_finding_is_never_visible_even_with_full_access():
    adapter = FixtureProactiveAdapter(FINDINGS, EVIDENCE)
    full_access = _case(
        identity=EvalIdentity(permissions=frozenset({"incident:read", "knowledge:review"}))
    )
    visible = await adapter.list_findings(full_access)
    assert "multi-document-with-deleted-evidence" not in visible


@pytest.mark.asyncio
async def test_cross_organization_finding_never_surfaces():
    adapter = FixtureProactiveAdapter(FINDINGS, EVIDENCE)
    full_access = _case(
        identity=EvalIdentity(permissions=frozenset({"incident:read", "knowledge:review"}))
    )
    visible = await adapter.list_findings(full_access)
    assert "other-org-recurring-severity" not in visible


@pytest.mark.asyncio
async def test_deterministic_runner_wires_the_proactive_category_end_to_end():
    """The shipped fixture corpus must actually be reachable through
    `build_deterministic_runner` -- not just importable in isolation."""
    runner = build_deterministic_runner(
        CORPUS,
        CANNED_ANSWERS,
        CANNED_INVESTIGATIONS,
        proactive_findings=FINDINGS,
        proactive_evidence=EVIDENCE,
    )
    case = _case(
        identity=EvalIdentity(permissions=frozenset({"incident:read"})),
        expected={"proactive": {"expected_labels": ["recurring-severity-platform"]}},
    )
    result = await runner.run_case(case)
    assert result.category == "proactive"
    assert result.passed is True
