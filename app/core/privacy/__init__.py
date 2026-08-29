"""core/privacy -- data-subject deletion (Priority 3 of the production-
maturity roadmap).

Owned by: core/privacy. Its scope is deliberately narrow: turning "delete
this person's data" into an explicit, ownership-aware, idempotent plan and
executing it. It owns no tables of its own -- deletion is inherently a
cross-module operation, so this module reads and writes tables defined in
`app.database.models.*` through its own `repository.py`, the same pattern
`core/knowledge` already uses to write `documents` (a table nominally owned
by `app.ingestion`).

**Why a new module rather than adding this to core/users**: deletion spans
auth artifacts, tenancy memberships, agent telemetry, and audit actor
strings. Putting it in `core/users` would make that module the de-facto
owner of every other module's deletion semantics; a separate module keeps
the ownership map (see `docs/DATA_LIFECYCLE.md`) in one readable place.

See `docs/DATA_LIFECYCLE.md` for the full data ownership map, the
delete/anonymize/retain classification for every table, and the explicit
list of what this implementation does NOT guarantee. Read that before
changing anything here: the classifications are derived from real foreign-key
constraints, not preference, and changing one without re-reading the schema
is how a deletion feature starts destroying organization data.

No GDPR/legal compliance is claimed anywhere in this module. It provides a
technical deletion mechanism; whether that mechanism satisfies any given
regulation is a legal determination this code cannot make.
"""
