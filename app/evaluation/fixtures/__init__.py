"""Fixtures for Mode 1 (deterministic) evaluation: a small synthetic corpus
(`corpus.py`), canned answer/investigation outputs keyed by case id
(`canned_generations.py`), and the four shipped regression datasets
(`retrieval_core_v1.jsonl`, `grounding_core_v1.jsonl`, `answer_core_v1.jsonl`,
`investigation_core_v1.jsonl`, each with a `.meta.json` sidecar).

All four datasets and the corpus/canned outputs describe one small, coherent
scenario -- an authentication service outage following a deployment that
shrank its database connection pool -- rather than unrelated one-off
questions, the same "one real scenario, not disconnected trivia" shape
`tests/rag_validation/rag_dataset.json` uses for its own real corpus. Stable
document/chunk ids are derived deterministically (`stable_uuid`) so every
fixture file, dataset case, and test can reference the same id without
generating and hand-copying real UUIDs.
"""

from __future__ import annotations

import uuid

_NAMESPACE = uuid.NAMESPACE_DNS


def stable_uuid(label: str) -> uuid.UUID:
    """Deterministic UUID5 for a human-readable fixture label -- the same
    label always produces the same UUID, so `corpus.py`, `canned_generations
    .py`, and the JSONL datasets can all independently reference e.g.
    `"incident-123"` and mean the same document."""
    return uuid.uuid5(_NAMESPACE, f"ekip-eval-fixture:{label}")
