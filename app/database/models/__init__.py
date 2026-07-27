"""ORM model groups, one file per owning module's table group.

Ownership stays visible in the filesystem (`core_models.py`,
`tenancy_models.py`, and future `agent_models.py`, `mcp_models.py`,
`ingestion_models.py`), mirroring DATABASE_DESIGN.md and PROJECT_PLAN.md.
Every model registers against the single `Base` in `app/database/session.py`
so Alembic's autogenerate sees the whole schema in one metadata object.
"""
