"""Embedding-based similarity-threshold clustering for the Knowledge Gap
Agent.

Resolves AGENT_WORKFLOWS.md's open item ("clustering method/threshold for
the Knowledge Gap Agent (`k`-means over embeddings vs. a simpler
similarity-threshold grouping) -- not yet decided"): similarity-threshold
(greedy leader-clustering) is chosen over k-means for a concrete reason, not
a coin flip -- k-means requires knowing the number of clusters, `k`, ahead
of time, and the number of distinct "gap topics" an organization has at any
given moment is exactly the unknown quantity this whole agent exists to
discover, not something a caller can reasonably supply as an input. Leader
clustering (compare each new point against every existing cluster's
centroid; join the closest one if it clears a similarity threshold, else
start a new cluster) naturally produces a variable number of clusters, runs
in a single pass with no iterative-refinement/convergence step to worry
about, and is simple enough to unit-test exhaustively and deterministically
(unlike k-means, whose random initialization makes an exact expected output
hard to pin down in a test). All the right tradeoffs for a periodic batch
job over a modest per-organization row count -- this is not a large-scale
clustering problem.

Pure, database-free, LLM-free logic -- exactly the same "pure/deterministic
and unit-testable with synthetic inputs" property AGENT_WORKFLOWS.md praises
the Confidence Evaluation Node for, deliberately carried over here.
"""

from __future__ import annotations

import math


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors. Returns `0.0` for
    a zero vector rather than dividing by zero -- an all-zero embedding
    should never occur in practice, but a defensive `0.0` (minimum possible
    similarity) is a safer failure mode here than an unhandled
    `ZeroDivisionError` crashing a whole clustering pass over one bad row.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class _Cluster:
    """One accumulating cluster: a running-sum centroid (recomputed lazily
    as an average) plus every original-list index it has absorbed so far.
    """

    def __init__(self, first_index: int, first_embedding: list[float]) -> None:
        self.member_indices: list[int] = [first_index]
        self._sum: list[float] = list(first_embedding)

    @property
    def centroid(self) -> list[float]:
        n = len(self.member_indices)
        return [value / n for value in self._sum]

    def add(self, index: int, embedding: list[float]) -> None:
        self.member_indices.append(index)
        self._sum = [s + e for s, e in zip(self._sum, embedding, strict=True)]


def cluster_by_similarity(
    embeddings: list[list[float]], *, similarity_threshold: float
) -> list[list[int]]:
    """Greedily group `embeddings` (referenced by their original list index)
    into clusters.

    For each embedding in input order: compare it against every existing
    cluster's centroid, join the single closest one if that similarity meets
    `similarity_threshold`, otherwise start a new cluster containing just
    this point. Order-dependent by design (a leader-clustering property, not
    a bug) -- a fixed input order always produces the same output, which is
    what makes this exhaustively unit-testable, unlike an order-independent
    but non-deterministic algorithm.

    Returns one list of original indices per cluster, in the order clusters
    were first created. An empty `embeddings` list returns `[]`.
    """
    clusters: list[_Cluster] = []
    for index, embedding in enumerate(embeddings):
        best_cluster: _Cluster | None = None
        best_similarity = -1.0
        for cluster in clusters:
            similarity = cosine_similarity(embedding, cluster.centroid)
            if similarity > best_similarity:
                best_similarity = similarity
                best_cluster = cluster

        if best_cluster is not None and best_similarity >= similarity_threshold:
            best_cluster.add(index, embedding)
        else:
            clusters.append(_Cluster(index, embedding))

    return [cluster.member_indices for cluster in clusters]
