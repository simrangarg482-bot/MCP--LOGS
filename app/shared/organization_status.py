"""One definition of what an organization's `status` means for access.

`organizations.status` is free text today (no database CHECK yet -- the lifecycle migration adds
one), so every reader normalizes the value the same way instead of comparing strings ad hoc.

Access policy (approved lifecycle):

* `onboarding` -- fully usable (it is where every self-service organization lives today).
* `active`     -- fully usable.
* `suspended`  -- no new sessions, and every already-issued access token is refused.

An unrecognized value is NOT treated as suspended (that would lock a customer out over a typo made
before the CHECK constraint exists); it is allowed, and callers log it so it gets fixed. Only the
normalized value `suspended` blocks access. Matching is case- and whitespace-insensitive, so
`" Suspended"` blocks as well.
"""

from __future__ import annotations

ONBOARDING = "onboarding"
ACTIVE = "active"
SUSPENDED = "suspended"

KNOWN_STATUSES = frozenset({ONBOARDING, ACTIVE, SUSPENDED})


def normalize(status: object) -> str:
    """Canonical form of a stored status ('' for anything that is not text)."""
    return status.strip().lower() if isinstance(status, str) else ""


def is_suspended(status: object) -> bool:
    return normalize(status) == SUSPENDED


def is_recognized(status: object) -> bool:
    return normalize(status) in KNOWN_STATUSES
