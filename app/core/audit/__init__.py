"""core/audit -- the append-only audit trail.

Owns the `audit_logs` table (DATABASE_DESIGN.md: "core/ -- owned tables").
Every mutating operation across core/ records an entry here, so audit is the
one submodule the others depend on -- it is implemented first for that reason.

Append-only by contract: this submodule's repository only ever INSERTs and
SELECTs `audit_logs`; it never updates or deletes a row. That rule lives here
in code, not at the ORM level, per DATABASE_DESIGN.md.

Callers use `from app.core.audit.service import record_audit_event`; this
package intentionally exposes nothing at import time.
"""