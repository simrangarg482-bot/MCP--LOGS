"""Structural assertions on the compiled agent graphs (`app.agents.graph`).

`build_graph` is deliberately a DAG with a single conditional fork and two
terminal branches -- an Answer/Investigation loop, or any re-entry into
Retrieval/Confidence, is impossible by construction. Nothing pinned that
before: a future edit could add an edge back to an earlier node (turning a
low-confidence answer into an unbounded re-investigation loop) with no test
catching it. These tests fail if the shape changes.

`build_graph(None, None)` is safe here: the node factories close over
`session`/`llm` but never touch them at construction time, only when the
compiled graph is actually invoked (which these tests never do).
"""

from __future__ import annotations

from app.agents.graph import build_graph, build_investigation_graph

_START = "__start__"
_END = "__end__"


def _edges(compiled) -> set[tuple[str, str]]:
    graph = compiled.get_graph()
    return {(edge.source, edge.target) for edge in graph.edges}


def test_answer_graph_has_exactly_the_expected_nodes() -> None:
    nodes = set(build_graph(None, None).get_graph().nodes.keys())

    assert nodes == {
        _START,
        _END,
        "retrieval_agent",
        "confidence_evaluation",
        "answer_agent",
        "investigation_agent",
    }


def test_answer_graph_is_linear_up_to_a_single_confidence_fork() -> None:
    edges = _edges(build_graph(None, None))

    assert (_START, "retrieval_agent") in edges
    assert ("retrieval_agent", "confidence_evaluation") in edges
    # the one and only fork
    assert ("confidence_evaluation", "answer_agent") in edges
    assert ("confidence_evaluation", "investigation_agent") in edges
    # both branches are terminal
    assert ("answer_agent", _END) in edges
    assert ("investigation_agent", _END) in edges


def test_answer_graph_has_no_cycles() -> None:
    edges = _edges(build_graph(None, None))

    # No edge points back to an earlier stage. Specifically: nothing re-enters
    # retrieval or confidence, and neither terminal agent feeds the other.
    forbidden = {
        ("confidence_evaluation", "retrieval_agent"),
        ("answer_agent", "retrieval_agent"),
        ("answer_agent", "confidence_evaluation"),
        ("answer_agent", "investigation_agent"),
        ("investigation_agent", "retrieval_agent"),
        ("investigation_agent", "confidence_evaluation"),
        ("investigation_agent", "answer_agent"),
    }
    assert forbidden.isdisjoint(edges)

    # General guard: a topological sort must exist (i.e. the directed graph
    # really is acyclic), computed straight from the edge list.
    _assert_acyclic(edges)


def test_answer_graph_every_node_can_reach_end() -> None:
    edges = _edges(build_graph(None, None))
    reachable_to_end = _nodes_that_reach(edges, _END)

    for node in ("retrieval_agent", "confidence_evaluation", "answer_agent", "investigation_agent"):
        assert node in reachable_to_end, f"{node} cannot reach {_END}"


def test_triage_graph_enters_investigation_directly_and_terminates() -> None:
    """`build_investigation_graph` (used by `triage_incident`) bypasses
    retrieval and confidence entirely -- it must contain only the
    investigation node, wired start -> investigation -> end.
    """
    compiled = build_investigation_graph(None, None)
    nodes = set(compiled.get_graph().nodes.keys())
    edges = _edges(compiled)

    assert nodes == {_START, _END, "investigation_agent"}
    assert edges == {
        (_START, "investigation_agent"),
        ("investigation_agent", _END),
    }
    assert "retrieval_agent" not in nodes
    assert "confidence_evaluation" not in nodes


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _assert_acyclic(edges: set[tuple[str, str]]) -> None:
    """Kahn's algorithm -- raises AssertionError if any cycle remains."""
    successors: dict[str, set[str]] = {}
    indegree: dict[str, int] = {}
    for source, target in edges:
        successors.setdefault(source, set()).add(target)
        indegree.setdefault(source, 0)
        indegree[target] = indegree.get(target, 0) + 1

    queue = [node for node, deg in indegree.items() if deg == 0]
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        for succ in successors.get(node, ()):
            indegree[succ] -= 1
            if indegree[succ] == 0:
                queue.append(succ)

    assert visited == len(indegree), "graph contains a cycle"


def _nodes_that_reach(edges: set[tuple[str, str]], target: str) -> set[str]:
    predecessors: dict[str, set[str]] = {}
    for source, dest in edges:
        predecessors.setdefault(dest, set()).add(source)

    seen: set[str] = set()
    stack = [target]
    while stack:
        node = stack.pop()
        for pred in predecessors.get(node, ()):
            if pred not in seen:
                seen.add(pred)
                stack.append(pred)
    return seen
