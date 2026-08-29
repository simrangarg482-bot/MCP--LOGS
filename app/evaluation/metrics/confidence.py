"""Confidence-calibration bucket analysis.

Distinct from `scripts/eval_confidence.py`'s threshold sweep: that script
asks "which threshold on the answer-vs-investigate decision maximizes F1."
This module asks the calibration question instead -- "when the system
reports confidence X, how often is it actually right" -- which needs no
notion of a routing threshold at all, only `(predicted_confidence,
was_correct)` pairs. Those pairs can come from this package's own
deterministic fixtures (`app.evaluation.runner`) or from
`app.evaluation.adapters.eval_confidence_report`, which reads
`eval_confidence.py`'s real JSON report and derives the same pairs from
genuine production runs -- see that adapter's module docstring.
"""

from __future__ import annotations

from app.evaluation.schemas import CalibrationBucket, CalibrationReport

#: Sensible default per this package's spec: ten equal-width buckets from
#: 0.0 to 1.0. Always overridable -- every public function here takes
#: `bucket_edges` explicitly, no hidden module-level default used silently.
DEFAULT_BUCKET_EDGES: tuple[float, ...] = tuple(round(i * 0.1, 1) for i in range(11))


def _bucket_index(confidence: float, bucket_edges: tuple[float, ...]) -> int:
    """Which bucket `confidence` falls into, clamping into range so a
    confidence of exactly `1.0` (or a value marginally above/below the
    configured range due to floating point) lands in the last/first bucket
    rather than raising.
    """
    last_bucket = len(bucket_edges) - 2
    for i in range(len(bucket_edges) - 1):
        low, high = bucket_edges[i], bucket_edges[i + 1]
        if low <= confidence < high or (i == last_bucket and confidence == high):
            return i
    return 0 if confidence < bucket_edges[0] else last_bucket


def compute_calibration(
    predictions: list[tuple[float, bool]],
    bucket_edges: tuple[float, ...] = DEFAULT_BUCKET_EDGES,
) -> CalibrationReport:
    """`predictions` is a list of `(predicted_confidence, was_correct)`
    pairs. "Correct" is caller-defined -- for an answer/route decision, it
    means "the system's decision matched ground truth"; for a plain answer
    case, it might mean "the assertions passed." This function only ever
    aggregates the pairs it's given; it has no opinion on what correctness
    means for a given case category.

    An empty `predictions` list produces a report with every bucket empty
    and `overall_calibration_error=None` -- not `0.0`, since "no calibration
    error measured" and "perfectly calibrated" are different claims.
    """
    if len(bucket_edges) < 2:
        raise ValueError("bucket_edges must have at least 2 edges (one bucket)")

    buckets_confidence: list[list[float]] = [[] for _ in range(len(bucket_edges) - 1)]
    buckets_correct: list[list[bool]] = [[] for _ in range(len(bucket_edges) - 1)]

    for confidence, was_correct in predictions:
        index = _bucket_index(confidence, bucket_edges)
        buckets_confidence[index].append(confidence)
        buckets_correct[index].append(was_correct)

    buckets: list[CalibrationBucket] = []
    weighted_gap_sum = 0.0
    total_count = 0

    for i in range(len(bucket_edges) - 1):
        confidences = buckets_confidence[i]
        corrects = buckets_correct[i]
        count = len(confidences)
        if count == 0:
            buckets.append(
                CalibrationBucket(
                    range_low=bucket_edges[i],
                    range_high=bucket_edges[i + 1],
                    count=0,
                    mean_predicted_confidence=None,
                    actual_success_rate=None,
                    calibration_gap=None,
                )
            )
            continue

        mean_confidence = sum(confidences) / count
        success_rate = sum(corrects) / count
        gap = abs(mean_confidence - success_rate)
        buckets.append(
            CalibrationBucket(
                range_low=bucket_edges[i],
                range_high=bucket_edges[i + 1],
                count=count,
                mean_predicted_confidence=mean_confidence,
                actual_success_rate=success_rate,
                calibration_gap=gap,
            )
        )
        weighted_gap_sum += gap * count
        total_count += count

    overall_error = (weighted_gap_sum / total_count) if total_count else None
    return CalibrationReport(
        buckets=buckets, overall_calibration_error=overall_error, sample_count=total_count
    )
