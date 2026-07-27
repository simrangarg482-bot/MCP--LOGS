"""Infrastructure layer: database engine, session lifecycle, ORM models, and
migrations.

Owned by: database/ (ARCHITECTURE.md section 3 -- infrastructure, no business
logic; sits below every other module and imports from none of them). Every
module that needs persistence goes through this package, never around it.

This is the canonical location for the persistence layer, per
ARCHITECTURE.md, PROJECT_STRUCTURE.md, DATABASE_DESIGN.md, and
PROJECT_PLAN.md -- all of which describe `app/database/`, not a path nested
under `shared/`. `database/` and `shared/` are two different things
(ARCHITECTURE.md section 3): `shared/` is cross-cutting utility code with no
business meaning of its own (config, logging), while `database/` is the
actual persistence infrastructure every other module depends on. Nesting one
inside the other conflated the two, which is why the models briefly lived
under `app/shared/config/Database/` -- that was a mismatch, not a design
choice, and has been consolidated here.
"""
