"""Tests for `app.shared.backoff.full_jitter_backoff_seconds` (Phase 6.2)."""

from __future__ import annotations

from app.shared.backoff import full_jitter_backoff_seconds


def test_delay_is_never_negative_and_never_exceeds_cap() -> None:
    for attempt in range(10):
        delay = full_jitter_backoff_seconds(attempt, cap=300.0)
        assert 0.0 <= delay <= 300.0


def test_delay_is_capped_even_for_large_attempts() -> None:
    delay = full_jitter_backoff_seconds(50, cap=300.0)

    assert delay <= 300.0


def test_zero_attempt_is_bounded_by_base() -> None:
    delay = full_jitter_backoff_seconds(0, base=1.0, cap=300.0)

    assert 0.0 <= delay <= 1.0


def test_successive_calls_are_not_all_identical() -> None:
    """The whole point of jitter -- pin down that it actually varies, not
    just that it's in range (a bug that always returned the same value
    within range would still pass the range-only assertions above).
    """
    delays = {full_jitter_backoff_seconds(5, cap=300.0) for _ in range(20)}

    assert len(delays) > 1
