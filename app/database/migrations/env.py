"""Alembic entrypoint (required literal filename).

Owned by: database/. `alembic.ini`'s `script_location` points at this
directory, and Alembic looks for a file literally named `env.py` inside it --
the actual migration-environment wiring (async engine, `Base.metadata`, the
offline/online migration runners) already lives in `base.py`. Rather than
duplicating that logic here, or renaming/deleting `base.py` (files already
written to the connected workspace folder can't be renamed or deleted
without asking first), this file just imports `base.py`: importing a module
executes its top-level code once as a side effect, which is exactly what
runs `run_migrations_offline()`/`run_migrations_online()` -- the same thing
Alembic's own generated `env.py` does directly, just one import away here
instead of inline.
"""

from app.database.migrations import base  # noqa: F401
