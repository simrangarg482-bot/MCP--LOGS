"""`app.shared.organization_status`: the one definition of what a status means for access."""

from __future__ import annotations

import pytest

from app.shared.organization_status import (
    ACTIVE,
    KNOWN_STATUSES,
    ONBOARDING,
    SUSPENDED,
    is_recognized,
    is_suspended,
    normalize,
)


def test_exactly_three_states() -> None:
    assert KNOWN_STATUSES == {ONBOARDING, ACTIVE, SUSPENDED} == {"onboarding", "active", "suspended"}


@pytest.mark.parametrize("value", ["suspended", "Suspended", "SUSPENDED", "  suspended  ", "\tSuspended\n"])
def test_suspended_is_matched_case_and_whitespace_insensitively(value) -> None:
    assert is_suspended(value) is True


@pytest.mark.parametrize("value", ["onboarding", "active", "Active", "", "suspend", "suspended-soon", "not suspended", None, 0, b"suspended"])
def test_everything_else_is_not_suspended(value) -> None:
    assert is_suspended(value) is False


@pytest.mark.parametrize("value", ["onboarding", "ACTIVE", " suspended "])
def test_known_values_are_recognized(value) -> None:
    assert is_recognized(value) is True


@pytest.mark.parametrize("value", ["trial", "archived", "", None, 3])
def test_unknown_values_are_not_recognized_but_never_count_as_suspended(value) -> None:
    """Before the CHECK constraint exists a typo must not lock a customer out: unknown is allowed
    (and logged by the caller), only `suspended` blocks."""
    assert is_recognized(value) is False and is_suspended(value) is False


def test_normalize_never_raises() -> None:
    assert normalize(None) == "" and normalize(5) == "" and normalize("  AcTiVe ") == "active"
