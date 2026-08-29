"""Mode 1 memory fixtures: a small set of labelled memories owned by two
different users, plus one project-scoped and one superseded/deleted memory.

Deliberately spans MORE than one owner. A memory corpus with a single owner
cannot express the failure this subsystem most needs a regression gate for --
one user's private memory surfacing for another user's semantically similar
query. Two owners is the minimum that makes that testable at all.

Embeddings use the same crude, deterministic bag-of-words scheme as
`tests/core/memory/test_service.py`: each vocabulary word owns one dimension,
so cosine distance is a real function of shared words and "relevant outranks
irrelevant" is a genuine assertion rather than a tautology. No model is
loaded, so this runs anywhere, including CI.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: One dimension per word. Matches `retrieval.embedding.EMBEDDING_DIMENSION`
#: in length so these vectors are interchangeable with real ones in shape.
EMBEDDING_DIMENSION = 384
_VOCAB = [
    "deployment",
    "database",
    "connection",
    "pool",
    "region",
    "europe",
    "billing",
    "invoice",
    "rollback",
    "postgres",
]

#: Stable identities referenced by the dataset's `identity.user_id`.
ALICE = "eval-user-alice"
BOB = "eval-user-bob"

#: The one project used by project-scoped fixtures. The dataset grants
#: membership by putting this in a case identity's `project_permissions`.
PROJECT_PLATFORM = "11111111-1111-5111-8111-111111111111"


def fake_embed(text: str) -> list[float]:
    """Deterministic bag-of-words embedding, L2-normalized."""
    vector = [0.0] * EMBEDDING_DIMENSION
    lowered = text.lower()
    for index, word in enumerate(_VOCAB):
        if word in lowered:
            vector[index] = 1.0
    if not any(vector):
        # Orthogonal to every vocabulary word, so an unrelated memory is
        # genuinely far from every query rather than accidentally near.
        vector[EMBEDDING_DIMENSION - 1] = 1.0
    norm = sum(v * v for v in vector) ** 0.5
    return [v / norm for v in vector]


@dataclass(frozen=True)
class MemoryFixture:
    """One pre-existing memory, addressed by `label`."""

    label: str
    content: str
    scope: str = "user"
    owner_user_id: str | None = ALICE
    project_id: str | None = None
    memory_type: str = "fact"
    #: `"active"` | `"superseded"` | `"deleted"`. Non-active fixtures exist so
    #: the dataset can assert they are excluded from recall -- the
    #: supersession and deletion regression cases.
    status: str = "active"
    embedding: list[float] = field(default_factory=list)

    def vector(self) -> list[float]:
        return self.embedding or fake_embed(self.content)


MEMORY_CORPUS: list[MemoryFixture] = [
    # --- Alice's private memories -----------------------------------------
    MemoryFixture(
        label="alice-pool-fact",
        content="The database connection pool was exhausted during the incident",
        owner_user_id=ALICE,
        memory_type="fact",
    ),
    MemoryFixture(
        label="alice-region-preference",
        content="Primary deployment region is europe",
        owner_user_id=ALICE,
        memory_type="preference",
    ),
    MemoryFixture(
        label="alice-billing-unrelated",
        content="Billing invoice templates were updated",
        owner_user_id=ALICE,
        memory_type="fact",
    ),
    # --- Bob's private memory, same topic as Alice's ----------------------
    # Same subject matter on purpose: if the visibility filter regressed,
    # Alice's query would surface this and vice versa.
    MemoryFixture(
        label="bob-pool-fact",
        content="Bob's private note about the database connection pool",
        owner_user_id=BOB,
        memory_type="fact",
    ),
    # --- Project-scoped, shared with platform members ---------------------
    MemoryFixture(
        label="platform-rollback-decision",
        content="The team decided rollback requires a postgres snapshot first",
        scope="project",
        owner_user_id=None,
        project_id=PROJECT_PLATFORM,
        memory_type="investigation_conclusion",
    ),
    # --- Lifecycle negatives ----------------------------------------------
    MemoryFixture(
        label="alice-stale-region",
        content="Primary deployment region is europe west legacy",
        owner_user_id=ALICE,
        memory_type="preference",
        status="superseded",
    ),
    MemoryFixture(
        label="alice-deleted-pool",
        content="Deleted note about the database connection pool",
        owner_user_id=ALICE,
        memory_type="fact",
        status="deleted",
    ),
]

MEMORY_BY_LABEL = {fixture.label: fixture for fixture in MEMORY_CORPUS}
