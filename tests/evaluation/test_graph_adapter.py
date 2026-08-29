"""Tests for `app.evaluation.adapters.graph` -- the permission-aware
traversal seam the "graph" evaluation category runs against.

Same shape as `test_retrieval_adapter.py`'s permission-aware filtering
requirement: the same origin, same fixture graph, different identity
permissions must produce a different reachable set.
"""

from __future__ import annotations

import pytest

from app.evaluation.adapters.graph import FixtureGraphAdapter
from app.evaluation.fixtures.canned_generations import CANNED_ANSWERS, CANNED_INVESTIGATIONS
from app.evaluation.fixtures.corpus import CORPUS
from app.evaluation.fixtures.graph_corpus import ENTITIES_BY_LABEL, GRAPH_EDGES
from app.evaluation.runner import build_deterministic_runner
from app.evaluation.schemas import EvalIdentity, EvaluationCase


def _case(**overrides) -> EvaluationCase:
    defaults = dict(id="t", category="graph", query="graph traversal")
    defaults.update(overrides)
    return EvaluationCase(**defaults)


@pytest.mark.asyncio
async def test_unknown_origin_label_yields_nothing():
    adapter = FixtureGraphAdapter(ENTITIES_BY_LABEL, GRAPH_EDGES)
    case = _case(origin_label="does-not-exist")
    assert await adapter.traverse(case, depth=2) == []


@pytest.mark.asyncio
async def test_no_origin_label_yields_nothing():
    adapter = FixtureGraphAdapter(ENTITIES_BY_LABEL, GRAPH_EDGES)
    case = _case(origin_label=None)
    assert await adapter.traverse(case, depth=2) == []


@pytest.mark.asyncio
async def test_permission_gates_whether_the_connected_incident_is_reachable():
    adapter = FixtureGraphAdapter(ENTITIES_BY_LABEL, GRAPH_EDGES)

    without_permission = _case(
        origin_label="runbook-pool-tuning", identity=EvalIdentity(permissions=frozenset())
    )
    with_permission = _case(
        origin_label="runbook-pool-tuning",
        identity=EvalIdentity(permissions=frozenset({"incident:read"})),
    )

    denied = await adapter.traverse(without_permission, depth=1)
    allowed = await adapter.traverse(with_permission, depth=1)

    assert "incident-pool-exhaustion" not in denied
    assert "incident-pool-exhaustion" in allowed


@pytest.mark.asyncio
async def test_deleted_entity_is_never_reachable_at_any_permission_level():
    adapter = FixtureGraphAdapter(ENTITIES_BY_LABEL, GRAPH_EDGES)
    full_access = _case(
        origin_label="incident-pool-exhaustion",
        identity=EvalIdentity(
            permissions=frozenset(
                {"incident:read", "postmortem:write", "postmortem:approve", "knowledge:review"}
            )
        ),
    )
    reached = await adapter.traverse(full_access, depth=2)
    assert "rejected-runbook" not in reached


@pytest.mark.asyncio
async def test_unpublished_document_requires_review_permission():
    adapter = FixtureGraphAdapter(ENTITIES_BY_LABEL, GRAPH_EDGES)
    reader_only = _case(
        origin_label="incident-pool-exhaustion",
        identity=EvalIdentity(permissions=frozenset({"incident:read"})),
    )
    reviewer = _case(
        origin_label="incident-pool-exhaustion",
        identity=EvalIdentity(permissions=frozenset({"incident:read", "knowledge:review"})),
    )

    without_review = await adapter.traverse(reader_only, depth=1)
    with_review = await adapter.traverse(reviewer, depth=1)

    assert "proposed-runbook-draft" not in without_review
    assert "proposed-runbook-draft" in with_review


@pytest.mark.asyncio
async def test_cross_organization_origin_resolves_to_nothing():
    """An origin belonging to a different organization than the case's own
    must not resolve at all, regardless of permissions."""
    adapter = FixtureGraphAdapter(ENTITIES_BY_LABEL, GRAPH_EDGES)
    case = _case(
        origin_label="other-org-incident",
        identity=EvalIdentity(permissions=frozenset({"incident:read"})),
    )
    assert await adapter.traverse(case, depth=2) == []


@pytest.mark.asyncio
async def test_depth_one_does_not_reach_two_hop_entities():
    adapter = FixtureGraphAdapter(ENTITIES_BY_LABEL, GRAPH_EDGES)
    case = _case(
        origin_label="runbook-pool-tuning",
        identity=EvalIdentity(
            permissions=frozenset({"incident:read", "postmortem:write", "postmortem:approve"})
        ),
    )
    reached = await adapter.traverse(case, depth=1)
    assert "incident-pool-exhaustion" in reached
    assert "postmortem-pool-exhaustion" not in reached  # two hops away


@pytest.mark.asyncio
async def test_symmetric_relationship_is_traversable_from_either_incident():
    adapter = FixtureGraphAdapter(ENTITIES_BY_LABEL, GRAPH_EDGES)
    permissions = EvalIdentity(permissions=frozenset({"incident:read"}))

    from_a = await adapter.traverse(
        _case(origin_label="incident-pool-exhaustion", identity=permissions), depth=1
    )
    from_b = await adapter.traverse(
        _case(origin_label="incident-related-timeout", identity=permissions), depth=1
    )

    assert "incident-related-timeout" in from_a
    assert "incident-pool-exhaustion" in from_b


@pytest.mark.asyncio
async def test_deterministic_runner_wires_the_graph_category_end_to_end():
    """The shipped fixture graph must actually be reachable through
    `build_deterministic_runner` -- not just importable in isolation."""
    runner = build_deterministic_runner(
        CORPUS,
        CANNED_ANSWERS,
        CANNED_INVESTIGATIONS,
        graph_entities=ENTITIES_BY_LABEL,
        graph_edges=GRAPH_EDGES,
    )
    case = _case(
        origin_label="incident-pool-exhaustion",
        identity=EvalIdentity(permissions=frozenset({"incident:read"})),
        expected={"graph": {"depth": 1, "expected_labels": ["platform-project"]}},
    )
    result = await runner.run_case(case)
    assert result.category == "graph"
    assert result.passed is True
