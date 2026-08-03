"""ORM model groups, one file per owning module's table group.

Ownership stays visible in the filesystem (`core_models.py`,
`tenancy_models.py`, `auth_models.py`, `ingestion_models.py`,
`retrieval_models.py`, `agent_models.py`, and future `mcp_models.py`),
mirroring DATABASE_DESIGN.md and PROJECT_PLAN.md. Every model registers
against the single `Base` in `app/database/session.py` so Alembic's
autogenerate sees the whole schema in one metadata object.
"""
