"""Tests for `app.evaluation.metrics.confidence`."""

from __future__ import annotations

from app.evaluation.metrics.confidence import compute_calibration


def test_perfectly_calibrated_bucket_has_zero_gap():
    # Every prediction near 0.85 confidence, and 85% of them actually correct.
    predictions = [(0.85, True)] * 17 + [(0.85, False)] * 3
    report = compute_calibration(predictions, bucket_edges=(0.8, 0.9))
    assert report.sample_count == 20
    bucket = report.buckets[0]
    assert bucket.count == 20
    assert bucket.mean_predicted_confidence == 0.85
    assert bucket.actual_success_rate == 0.85
    assert abs(bucket.calibration_gap) < 1e-9
    assert abs(report.overall_calibration_error) < 1e-9


def test_overconfident_bucket_has_positive_gap():
    # Predicted 0.9, but only ever right half the time.
    predictions = [(0.9, True), (0.9, False)]
    report = compute_calibration(predictions, bucket_edges=(0.8, 1.0))
    bucket = report.buckets[0]
    assert bucket.mean_predicted_confidence == 0.9
    assert bucket.actual_success_rate == 0.5
    assert abs(bucket.calibration_gap - 0.4) < 1e-9


def test_empty_predictions_produce_empty_buckets_and_none_error():
    report = compute_calibration([])
    assert report.sample_count == 0
    assert report.overall_calibration_error is None
    assert all(bucket.count == 0 for bucket in report.buckets)
    assert all(bucket.mean_predicted_confidence is None for bucket in report.buckets)


def test_default_buckets_span_zero_to_one_in_tenths():
    report = compute_calibration([])
    assert len(report.buckets) == 10
    assert report.buckets[0].range_low == 0.0
    assert report.buckets[-1].range_high == 1.0


def test_confidence_of_exactly_one_lands_in_last_bucket():
    report = compute_calibration([(1.0, True)])
    last_bucket = report.buckets[-1]
    assert last_bucket.count == 1


def test_overall_error_is_sample_weighted_across_buckets():
    # Bucket A: 10 samples, gap 0.0. Bucket B: 1 sample, gap 1.0.
    # Weighted mean should be much closer to 0 than a naive bucket-average.
    predictions = [(0.05, False)] * 10 + [(0.95, False)]
    report = compute_calibration(predictions)
    assert report.overall_calibration_error is not None
    assert report.overall_calibration_error < 0.5


def test_bucket_edges_must_have_at_least_two_values():
    import pytest

    with pytest.raises(ValueError):
        compute_calibration([], bucket_edges=(0.5,))
