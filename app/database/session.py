"""Async SQLAlchemy engine, session management, and declarative base.

Owned by: database/ (ARCHITECTURE.md section 3 -- infrastructure layer, no
business logic, sits below every other module and can call nothing).

Every module that needs a DB session gets one through `get_db_session`
(the FastAPI dependency) or `session_scope` (for non-request contexts, e.g.
arq worker jobs) -- nothing outside this file constructs a session directly.
This is what keeps session lifecycle (commit/rollback/close) consistent
regardless of which module is doing the querying.

Cross-module discipline reminder (ARCHITECTURE.md section 2): a session
created here must never be passed across a module's public interface boundary
-- e.g. `core.get_incident(...)` returns a Pydantic model, not an ORM object
still attached to a session. That rule is enforced by convention in the
modules that use this file, not by anything in this file itself.
"""

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings

logger = get_logger(__name__)

# The Postgres GUC every Milestone 10 RLS policy checks against (see
# app/database/migrations/versions/c7d4e8f19a2b_milestone_10_row_level_security.py).
# Named here, not just there, since this module is the one place that ever
# sets it.
_TENANT_GUC_NAME = "app.current_organization_id"

# Query parameters libpq-style clients (psql, psycopg) understand but
# asyncpg's connect() does not -- SQLAlchemy's asyncpg dialect forwards every
# URL query parameter straight through as a keyword argument to
# asyncpg.connect(), so an unrecognized one raises a bare
# `TypeError: connect() got an unexpected keyword argument '...'` rather than
# a clear connection error. Neon's default copy-paste connection string
# includes `sslmode`/`channel_binding` by default; `.env.example` has long
# used the `ssl=` spelling. TLS itself is still applied -- via `connect_args`
# in `_build_engine`, using the `ssl` keyword asyncpg actually accepts, with
# its value derived from these same parameters by `_ssl_connect_arg` below.
_UNSUPPORTED_ASYNCPG_QUERY_PARAMS = {"sslmode", "channel_binding", "ssl"}

# `sslmode`/`ssl` values that select a plaintext connection. Everything else
# (including an omitted parameter) keeps TLS on: every managed provider this
# project has targeted requires it, so the secure default must survive a
# missing parameter. Railway's private network is the one place TLS is turned
# off on purpose -- traffic never leaves the project's internal network and
# the stock Postgres image serves no certificate -- via `?sslmode=disable`.
_SSL_DISABLED_VALUES = {"disable", "disabled", "false", "0", "off", "no"}


def _normalize_database_url(raw_url: str) -> str:
    """Strip libpq-only query parameters from a connection string before
    handing it to asyncpg.

    A permanent fix rather than asking every developer to hand-edit their
    `DATABASE_URL` to remove them: a connection string copy-pasted directly
    from Neon's dashboard should just work, unmodified.
    """
    parsed = urlsplit(raw_url)
    kept_params = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _UNSUPPORTED_ASYNCPG_QUERY_PARAMS
    ]
    return urlunsplit(parsed._replace(query=urlencode(kept_params)))


def _ssl_connect_arg(raw_url: str) -> bool:
    """Decide the `ssl` keyword `asyncpg.connect()` receives, from the URL's
    `sslmode` (preferred) or `ssl` query parameter.

    Returns `True` (TLS on) unless the parameter is explicitly one of
    `_SSL_DISABLED_VALUES` -- so an unset parameter stays secure, a managed
    provider's `?sslmode=require` keeps working unchanged, and only a
    deliberate `?sslmode=disable` (Railway private networking) turns TLS off.
    """
    params = {
        key.lower(): value
        for key, value in parse_qsl(urlsplit(raw_url).query, keep_blank_values=True)
    }
    requested = params.get("sslmode") or params.get("ssl") or ""
    return requested.strip().lower() not in _SSL_DISABLED_VALUES


class Base(DeclarativeBase):
    """Declarative base for every ORM model in app/database/models/.

    A single shared Base (rather than one per owning module) is deliberate:
    it's what lets Alembic's autogenerate see the entire schema in one
    metadata object, even though DATABASE_DESIGN.md's ownership rules keep
    each table's *write access* scoped to one module by convention.
    """


# Phase 6.1: bounds how long any single query/statement may run once a
# connection is established -- asyncpg's own `command_timeout`, distinct
# from the connection-establishment timeout (asyncpg's own `connect()`
# default, 60s, already bounded independently of this codebase). Without
# this, a hung/slow query (a lock wait, a runaway analytical query) had no
# application-level bound at all. 30s matches the flat timeout every
# ingestion connector already uses (`httpx.AsyncClient(..., timeout=30.0)`)
# -- generous for this application's normal OLTP-shaped queries, not
# generous enough to let a truly stuck connection hold a pool slot forever.
_COMMAND_TIMEOUT_SECONDS = 30.0
# Recycle pooled connections after 30 minutes -- `pool_pre_ping=True`
# already tests a connection is alive before handing it out, but does not
# bound how *old* a still-alive connection is; a managed Postgres provider
# (Neon) can terminate idle server-side connections on its own schedule
# independent of what the client believes is still open. Recycling
# proactively avoids relying solely on pre-ping's reactive detection.
_POOL_RECYCLE_SECONDS = 1800


def _build_engine() -> AsyncEngine:
    """Create the async engine from settings.

    A function rather than a module-level constant so tests can call it
    again after monkeypatching `Settings.database_url` (via
    `get_settings.cache_clear()`), consistent with the pattern already
    established in settings.py.
    """
    settings = get_settings()
    return create_async_engine(
        _normalize_database_url(str(settings.database_url)),
        # SQL echo is intentionally independent from ENVIRONMENT. A full
        # ingestion emits several statements per document; synchronously
        # printing all of them can become a measurable local bottleneck.
        echo=settings.database_echo,
        pool_pre_ping=True,
        pool_recycle=_POOL_RECYCLE_SECONDS,
        # asyncpg wants TLS as a connect-time keyword, not a URL query
        # parameter (see `_normalize_database_url` above). The value is
        # derived from the URL's own `sslmode`/`ssl` parameter by
        # `_ssl_connect_arg`: `True` for Neon and every other managed
        # provider (the default when unspecified), `False` only for a
        # deliberate `?sslmode=disable` such as Railway private networking,
        # where a non-SSL Postgres would fail outright with `ssl=True` forced.
        connect_args={
            "ssl": _ssl_connect_arg(str(settings.database_url)),
            "command_timeout": _COMMAND_TIMEOUT_SECONDS,
        },
    )


engine: AsyncEngine = _build_engine()
# Logged unconditionally at import time (not just on error) since this
# project runs against multiple, easily-confused DATABASE_URLs depending on
# how the process was started (root `.env` -> Neon vs `.env.docker` -> local
# Postgres container, see `.env.docker.example`'s own warning) -- printing
# host/dbname (never credentials) here makes "which database did this
# process actually connect to" a one-line grep instead of a debugging session.
logger.info(
    "database_engine_created",
    host=engine.url.host,
    database=engine.url.database,
)

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields a session, commits on success, rolls back
    and re-raises on exception, always closes.

    Usage in a module's repository.py:
        async def get_incident(session: AsyncSession = Depends(get_db_session)):
            ...
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            # `BaseException`, not `Exception`: `asyncio.CancelledError` (e.g.
            # an arq `job_timeout` firing mid-job) is a `BaseException` in
            # Python >=3.8 and was previously falling through this handler
            # uncaught, skipping the rollback and leaving the transaction
            # open on the server until the connection was eventually
            # recycled -- exactly the kind of idle-in-transaction state that
            # can block a later job's writes to the same rows.
            await session.rollback()
            raise
        finally:
            await session.close()


async def set_tenant_context(session: AsyncSession, organization_id: uuid.UUID) -> None:
    """Set the session-local GUC every Milestone 10 RLS policy checks
    (`app.current_organization_id`) for the remainder of the *current
    transaction only*.

    Uses `set_config(name, value, is_local=true)` rather than a literal
    `SET LOCAL app.current_organization_id = '<uuid>'` string -- `SET` is not
    a regular SQL statement and does not accept bind parameters, so building
    it via string interpolation would be the one place in this codebase
    doing that; `set_config` is an ordinary function call and takes a normal
    bound parameter instead. The third argument, `true`, is what makes this
    "local" (scoped to the current transaction, cleared on commit/rollback)
    rather than "session" (would otherwise leak forward to whatever the next
    request/job on this pooled connection happens to be, since sessions
    aren't guaranteed to map 1:1 to connections).

    Callers, in the order Identity/org context becomes known in this
    codebase: `app.api.deps.get_current_identity` (REST, every request),
    `app.mcp.auth.resolve_mcp_identity` (MCP, every call),
    `app.core.tenancy.service.evaluate_provisioning` and
    `get_organization_sso_config` (pre-Identity, login-flow paths that
    resolve `organization_id` themselves before any token exists), and
    `app.agents.workers.tasks`'s scheduled Knowledge Gap scan (each
    per-organization iteration, using the `organization_id` the scan loop
    already holds). `app.ingestion.service._execute_ingestion_job` is the
    one exception that cannot call this first -- seeing this module's own
    docstring and that function's docstring for why it resolves the org id
    via a narrow RLS-bypassing lookup before it can call this at all.

    Must be called on every session before any RLS-protected table is
    queried on it -- a session that never calls this sees zero rows from
    every such table (fail-closed; see the RLS migration's own docstring).
    """
    await session.execute(
        text("SELECT set_config(:guc_name, :org_id, true)"),
        {"guc_name": _TENANT_GUC_NAME, "org_id": str(organization_id)},
    )


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Context-manager equivalent of `get_db_session` for non-request
    contexts -- arq job handlers (ENGINEERING_DECISIONS.md #002), the
    Knowledge Gap Agent's scheduled graph, CLI scripts, etc. -- anywhere
    FastAPI's `Depends` isn't available.

    Usage:
        async with session_scope() as session:
            ...
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:  # see get_db_session's matching handler above
            await session.rollback()
            raise
        finally:
            await session.close()
