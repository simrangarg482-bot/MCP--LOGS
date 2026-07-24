"""Cross-module shared value types.

Owned by: shared/ (ARCHITECTURE.md section 3 -- cross-cutting, no business
meaning of its own, importable by every module; imports from none of them).

These are the literal domain vocabularies used verbatim across REST bodies,
internal interfaces, and MCP contracts (API_DESIGN.md "Design conventions":
defined once, reused everywhere, never redefined per-layer). Keeping them here
rather than in core/ avoids core/ becoming an import target for modules that
only need the vocabulary, not core's logic.
"""

from __future__ import annotations

from typing import Literal, TypedDict

# --- Domain vocabularies (API_DESIGN.md section 1) -----------------------
Severity = Literal["low", "medium", "high", "critical"]
IncidentStatus = Literal["open", "investigating", "resolved", "closed"]
PostmortemStatus = Literal["draft", "in_review", "approved", "published"]
DocumentStatus = Literal["proposed", "published"]
ActionItemStatus = Literal["open", "in_progress", "done"]

# Where an agent execution / audited action originated (DATABASE_DESIGN.md:
# agent_executions.trigger_source, and the same vocabulary reused elsewhere).
TriggerSource = Literal["mcp", "core_api", "scheduled"]


class ErrorBody(TypedDict, total=False):
    """The single error shape from API_DESIGN.md "Design conventions".

    A TypedDict rather than a Pydantic model because it's a transport-layer
    contract shaped by the API/MCP boundary, not a domain entity -- the actual
    Pydantic response envelope that carries it lives in the api/ layer (not yet
    built), so this stays a plain structural type the service layer can raise
    into without importing anything transport-specific.

    Fields:
        error_code: stable machine-readable code (e.g. "incident.not_found").
        message: human-readable summary.
        detail: optional structured context (field errors, ids, etc.).
    """

    error_code: str
    message: str
    detail: dict | None