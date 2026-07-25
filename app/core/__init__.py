"""core/ -- the transactional domain module.

Owns auth, RBAC, incidents, and audit, plus the tables listed under
"core/ -- owned tables" in DATABASE_DESIGN.md. This is the only module
permitted to write to those tables; every other module reads through the
public interface exposed here (never by importing core/'s repositories or ORM
models directly), per ARCHITECTURE.md section 3.

Boundary rules in force for this module:
  - core/ MUST NOT import from mcp/ or ingestion/ (enforced by the
    import-linter contract in pyproject.toml). It may use database/ and
    shared/, and it may call agents/ through agents/'s public interface.
  - Cross-module calls pass plain Pydantic data and an `Identity`, never a
    live SQLAlchemy session or an ORM object (ARCHITECTURE.md section 2), so
    this module stays extractable into its own service later.

Public interface note:
    core/'s callable surface (authorize, record_audit_event, create_incident,
    get_incident, update_incident, add_timeline_note, approve_postmortem,
    publish_document -- see API_DESIGN.md section 2) will be re-exported from
    this module once the corresponding submodule services are implemented.
    It is intentionally left empty for now: re-exporting service functions
    before their submodules exist would create import cycles and execute
    submodule code at package-import time. Until then, import from the owning
    submodule directly (e.g. `from app.core.audit.service import
    record_audit_event`).
"""