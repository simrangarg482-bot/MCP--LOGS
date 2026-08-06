"""Tests for `app.agents.knowledge_gap.clustering` -- pure, deterministic
logic, exercised with synthetic embeddings (no real model, no I/O), the
same "pure/deterministic and unit-testable" property this module's own
docstring claims for itself.
"""

from __future__ import annotations

import math

from app.agents.knowledge_gap.clustering import cluster_by_similarity, cosine_similarity


def test_cosine_similarity_identical_vectors_is_one() -> None:
    assert math.isclose(cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0)


def test_cosine_similarity_orthogonal_vectors_is_zero() -> None:
    assert math.isclose(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0)


def test_cosine_similarity_opposite_vectors_is_negative_one() -> None:
    assert math.isclose(cosine_similarity([1.0, 0.0], [-1.0, 0.0]), -1.0)


def test_cosine_similarity_zero_vector_returns_zero_not_crash() -> None:
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_empty_embeddings_returns_no_clusters() -> None:
    assert cluster_by_similarity([], similarity_threshold=0.8) == []


def test_single_embedding_forms_one_cluster() -> None:
    result = cluster_by_similarity([[1.0, 0.0]], similarity_threshold=0.8)
    assert result == [[0]]


def test_identical_embeddings_join_one_cluster() -> None:
    embeddings = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]
    result = cluster_by_similarity(embeddings, similarity_threshold=0.9)
    assert result == [[0, 1, 2]]


def test_dissimilar_embeddings_form_separate_clusters() -> None:
    embeddings = [[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]]
    result = cluster_by_similarity(embeddings, similarity_threshold=0.9)
    assert result == [[0], [1], [2]]


def test_two_distinct_groups_form_two_clusters() -> None:
    embeddings = [
        [1.0, 0.0],   # group A
        [0.99, 0.01], # group A (near-identical)
        [0.0, 1.0],   # group B
        [0.01, 0.99], # group B (near-identical)
    ]
    result = cluster_by_similarity(embeddings, similarity_threshold=0.9)
    assert result == [[0, 1], [2, 3]]


def test_threshold_boundary_below_threshold_starts_new_cluster() -> None:
    # cosine similarity between [1,0] and [0.5, 0.5] (unnormalized) ~ 0.707
    embeddings = [[1.0, 0.0], [0.5, 0.5]]
    result = cluster_by_similarity(embeddings, similarity_threshold=0.9)
    assert result == [[0], [1]]


def test_threshold_boundary_above_threshold_joins_cluster() -> None:
    embeddings = [[1.0, 0.0], [0.5, 0.5]]
    result = cluster_by_similarity(embeddings, similarity_threshold=0.7)
    assert result == [[0, 1]]


def test_exact_tie_between_clusters_joins_the_earlier_created_one() -> None:
    """[1,0] and [0,1] are orthogonal (similarity 0), so they never join
    each other and instead seed two separate singleton clusters. A third
    point equidistant from both, [0.5, 0.5], produces an exact similarity
    tie (~0.707 to each) -- confirms the deterministic tie-break rule
    (`>`, not `>=`, when picking the best cluster) keeps the earlier-created
    cluster, rather than the tie being resolved arbitrarily by dict/set
    ordering.
    """
    embeddings = [[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]]
    result = cluster_by_similarity(embeddings, similarity_threshold=0.5)
    assert result == [[0, 2], [1]]


def test_new_point_joins_closest_of_multiple_existing_clusters() -> None:
    embeddings = [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.9, 0.1, 0.0],  # much closer to cluster 0 than cluster 1
    ]
    result = cluster_by_similarity(embeddings, similarity_threshold=0.5)
    assert result == [[0, 2], [1]]
