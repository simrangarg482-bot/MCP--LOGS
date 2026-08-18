"""Exponential backoff with jitter (Phase 6.2).

Owned by: shared/ (cross-cutting, no business meaning of its own -- same
category as `logging.py`/`tracing.py`).

Every retry site in this codebase (`app.ingestion.workers.tasks`,
`app.agents.workers.tasks`, `app.agents.retry`) previously computed a pure
`min(2**attempt, cap)` backoff with no randomization. That's fine in
isolation, but correlated failures -- an OpenAI-wide outage, a Redis blip
affecting every in-flight arq job at once -- previously meant every affected
task/job retried at the *exact same* intervals, arriving back at the
recovering dependency in synchronized waves ("thundering herd") rather than
spread out. "Full jitter" (`random.uniform(0, min(cap, base * 2**attempt))`,
the AWS Architecture Blog's own recommended default over "equal jitter" or
no jitter at all) fixes this by randomizing the actual delay within the same
exponential envelope, without weakening the backoff's own growth curve or
its cap.
"""

from __future__ import annotations

import random


def full_jitter_backoff_seconds(attempt: int, *, base: float = 1.0, cap: float) -> float:
    """Return a randomized backoff delay for `attempt` (starting at 0 or 1,
    caller's convention -- this function doesn't care, it's a pure
    exponent), bounded by `cap`.

    `random.uniform(0, ...)`, not `random.uniform(cap/2, ...)` ("equal
    jitter") -- full jitter has the lowest expected total retry time across
    a fleet of retrying callers for the same bounded backoff envelope,
    which matters more here than any single caller's own delay being
    "smoother."
    """
    exponential_delay = min(cap, base * (2**attempt))
    return random.uniform(0, exponential_delay)
