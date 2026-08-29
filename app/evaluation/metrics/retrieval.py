"""Rank-aware retrieval metrics, independent of any generation step.

Every function here takes `retrieved_ids` (already in ranked order -- index 0
is the top result, matching `ScoredChunk`/`retrieval.service.search`'s own
already-ranked return order) and `relevant_ids` (the case's ground-truth
set, order-independent). None of these duplicate `retrieval.ranking.fusion`'s
reciprocal-rank-fusion logic -- fusion decides *what order results come back
in*; these metrics only ever grade an already-produced ordering against
ground truth.

`k` is always a required, explicit parameter -- no module-level default is
baked in, per this package's "no magic numbers" requirement. Callers
(`app.evaluation.runner`) sweep a configurable list of `k` values, matching
`scripts/eval_confidence.py`'s own precedent of a configurable threshold
list rather than one hardcoded number.
"""

from __future__ import annotations


def _top_k(retrieved_ids: list[str], k: int) -> list[str]:
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    return retrieved_ids[:k]


def recall_at_k(retrieved_ids: list[str], relevant_ids: set[str], k: int) -> float | None:
    """Fraction of `relevant_ids` present in the top `k` of `retrieved_ids`.

    Returns `None` (not `0.0` or `1.0`) when `relevant_ids` is empty -- a
    case with no expected-relevant documents at all is asserting something
    else entirely (or nothing), and silently scoring it as "perfect recall"
    or "total failure" would be a fabricated number, not a measurement.
    Callers must treat `None` as "not applicable to this case," not as a
    passing or failing score.
    """
    if not relevant_ids:
        return None
    found = set(_top_k(retrieved_ids, k)) & relevant_ids
    return len(found) / len(relevant_ids)


def precision_at_k(retrieved_ids: list[str], relevant_ids: set[str], k: int) -> float | None:
    """Fraction of the top `k` retrieved documents that are relevant.

    Returns `None` when nothing was retrieved at all within the top `k`
    (division by zero would otherwise silently become `0/0`) -- distinct
    from "0 relevant among N retrieved," which is a real `0.0` precision.
    """
    top = _top_k(retrieved_ids, k)
    if not top:
        return None
    found = sum(1 for doc_id in top if doc_id in relevant_ids)
    return found / len(top)


def mean_reciprocal_rank(retrieved_ids: list[str], relevant_ids: set[str]) -> float | None:
    """`1 / rank` of the first relevant document in `retrieved_ids` (rank is
    1-indexed), or `0.0` if none of `retrieved_ids` is relevant. Named "mean"
    for consistency with the standard metric name (MRR is a mean *across
    queries*); this function computes one query's reciprocal rank -- the
    runner averages this across a dataset's cases to get the actual MRR.

    Returns `None` when `relevant_ids` is empty, same reasoning as
    `recall_at_k`.
    """
    if not relevant_ids:
        return None
    for rank, doc_id in enumerate(retrieved_ids, start=1):
        if doc_id in relevant_ids:
            return 1.0 / rank
    return 0.0


def relevant_document_coverage(retrieved_ids: list[str], relevant_ids: set[str]) -> float | None:
    """Fraction of `relevant_ids` found *anywhere* in `retrieved_ids`
    (unbounded by any `k` -- this is `recall_at_k` with `k = len(retrieved_ids)`,
    named separately per this package's spec since "was every important
    document retrieved at all, regardless of rank" is a distinct question
    from "was it retrieved near the top").
    """
    if not relevant_ids:
        return None
    found = set(retrieved_ids) & relevant_ids
    return len(found) / len(relevant_ids)


def mean_of(values: list[float | None]) -> float | None:
    """Mean of the non-`None` values in `values`, or `None` if every value
    is `None` -- the runner's aggregation helper for turning a per-case
    metric list (recall_at_k across every case in a dataset) into one
    dataset-level number, consistently skipping "not applicable" cases
    rather than treating them as zero.
    """
    applicable = [v for v in values if v is not None]
    if not applicable:
        return None
    return sum(applicable) / len(applicable)
