"""Pydantic / value types genuinely shared across modules.

Owned by: shared/ (ARCHITECTURE.md section 3). Only types referenced from
more than one module belong here (`Identity`, the domain vocabularies); types
local to a single module live in that module's own schemas.py.

Re-exported here so callers write `from app.shared.schemas import Identity`
rather than reaching into submodules -- the submodule layout can then change
without breaking every import site.
"""

from app.shared.schemas.common import (
    ActionItemStatus,
    DocumentStatus,
    ErrorBody,
    IncidentStatus,
    PostmortemStatus,
    Severity,
    TriggerSource,
)
from app.shared.schemas.identity import ActorKind, Identity

__all__ = [
    "ActionItemStatus",
    "ActorKind",
    "DocumentStatus",
    "ErrorBody",
    "Identity",
    "IncidentStatus",
    "PostmortemStatus",
    "Severity",
    "TriggerSource",
]