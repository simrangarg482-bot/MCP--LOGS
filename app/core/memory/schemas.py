"""Pydantic contracts for core/memory.

Owned by: core/memory. Same "one schemas.py per module" convention as every
other `core/*` submodule.

Scope/type vocabularies live here rather than as database CHECK constraints,
matching this schema's existing convention for `documents.status`,
`incidents.status`, `invitations.status` and every other status-like column.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Implemented scopes, and only those.
#:
#: `"user"`    -- private to one person within one organization. Retrievable
#:                by that person alone.
#: `"project"` -- shared with everyone holding a membership in that project.
#:
#: DELIBERATELY ABSENT, with reasons (see `docs/AGENT_MEMORY.md`):
#:   `"organization"` -- org-wide memory every member sees overlaps almost
#:      exactly with the existing human-reviewed knowledge system
#:      (`documents` + `knowledge:review`), which already has a review gate,
#:      versioning and citations. Adding a parallel, uncited org-wide store
#:      is how memory turns into the second knowledge base this module exists
#:      not to be. Deferred as a product decision, not an oversight.
#:   `"conversation"` -- there is no conversation. Verified across the whole
#:      repository: no thread/session identifier exists anywhere (no
#:      LangGraph checkpointer, no `thread_id`, nothing on `AskRequest` or
#:      `agent_executions`). A conversation scope would need a conversation
#:      concept invented first; scoping to something that does not exist
#:      would be fiction.
MemoryScope = Literal["user", "project"]

#: Coarse category. Kept small on purpose -- a large speculative taxonomy
#: would be unfalsifiable at this stage.
#:   `"preference"`               -- how this person wants EKIP to behave.
#:   `"fact"`                     -- a durable statement about their systems.
#:   `"investigation_conclusion"` -- something an investigation established.
MemoryType = Literal["preference", "fact", "investigation_conclusion"]

#: `"active"` is the only status `recall` ever returns.
#: `"superseded"` -- replaced by a newer memory; kept for provenance.
#: `"deleted"`    -- tombstone. `content`/`embedding` are cleared when this
#:                   is set, so nothing recallable survives it.
MemoryStatus = Literal["active", "superseded", "deleted"]

#: Provenance. `"explicit"` means a human asked for this to be remembered --
#: which is the honest answer for the only creation path implemented today,
#: rather than manufacturing a richer-looking source.
MemorySourceType = Literal["explicit", "investigation", "incident"]

#: A single memory is one embedding, so its content has to fit one. Also the
#: per-memory half of the context budget: 2000 characters is roughly 500
#: tokens by `context_assembly`'s own 4-chars-per-token estimate, so even a
#: full complement of recalled memories cannot crowd out retrieved evidence.
MAX_CONTENT_LENGTH = 2000


class MemoryCreate(BaseModel):
    """Request to remember something.

    Note what is absent: no `organization_id`. Tenancy comes from the
    caller's resolved `Identity`, never from the request body -- the same
    rule `app.core.privacy` follows, and the reason a client cannot target
    another organization even by guessing ids.
    """

    model_config = ConfigDict(frozen=True)

    scope: MemoryScope
    memory_type: MemoryType
    content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)
    #: Required when `scope="project"`, forbidden otherwise -- enforced below.
    project_id: uuid.UUID | None = None
    source_type: MemorySourceType = "explicit"
    source_id: uuid.UUID | None = None
    #: When replacing an existing memory: the old one is marked
    #: `"superseded"` and the new row records this back-reference.
    supersedes_memory_id: uuid.UUID | None = None
    memory_metadata: dict | None = None

    @field_validator("content")
    @classmethod
    def _content_is_not_only_whitespace(cls, content: str) -> str:
        if not content.strip():
            raise ValueError("content cannot be empty or whitespace-only")
        return content

    @model_validator(mode="after")
    def _scope_matches_its_required_field(self) -> MemoryCreate:
        """A scope and its owning column must agree.

        `scope="user"` derives its owner from the actor (so no `project_id`
        may be supplied), and `scope="project"` is meaningless without the
        project it is shared with. Rejecting the mismatch here means the
        repository never has to reason about a half-specified scope -- and,
        more importantly, that a `"project"` memory can never be written with
        a NULL `project_id`, which `recall` would then have to decide what to
        do with.
        """
        if self.scope == "project" and self.project_id is None:
            raise ValueError("scope='project' requires project_id")
        if self.scope == "user" and self.project_id is not None:
            raise ValueError(
                "scope='user' memory is private to the actor and cannot carry a project_id"
            )
        return self


class MemoryUpdate(BaseModel):
    """Edit a memory's content in place.

    Content-only by design. Scope and ownership are deliberately immutable:
    allowing an update to change `scope` would be a silent privilege change
    (a private memory quietly becoming project-visible), which
    `docs/AGENT_MEMORY.md` requires to be an explicit, authorized
    supersession instead. Changing content re-embeds the row, so retrieval
    reflects the edit immediately.
    """

    model_config = ConfigDict(frozen=True)

    content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)
    memory_metadata: dict | None = None

    @field_validator("content")
    @classmethod
    def _content_is_not_only_whitespace(cls, content: str) -> str:
        if not content.strip():
            raise ValueError("content cannot be empty or whitespace-only")
        return content


class Memory(BaseModel):
    """A memory as returned to a caller.

    Carries no `embedding`: it is a 384-float internal detail with no meaning
    to any consumer, and omitting it keeps API responses (and logs) small --
    the same reasoning `ScoredChunk` uses for never returning raw vectors.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    scope: MemoryScope
    owner_user_id: uuid.UUID | None
    project_id: uuid.UUID | None
    memory_type: MemoryType
    content: str
    source_type: MemorySourceType | None
    source_id: uuid.UUID | None
    created_by: str
    status: MemoryStatus
    supersedes_memory_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class RecalledMemory(BaseModel):
    """One memory selected for injection, with its relevance score.

    `distance` is pgvector's cosine distance (0 = identical, 2 = opposite);
    `relevance` is the `1 - distance` convenience the threshold is expressed
    against, so configuration reads as "at least this similar" rather than
    "at most this far".
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    scope: MemoryScope
    memory_type: MemoryType
    content: str
    distance: float

    @property
    def relevance(self) -> float:
        return 1.0 - self.distance
