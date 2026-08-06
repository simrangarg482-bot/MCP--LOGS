"""Standalone connectivity diagnostic for DATABASE_URL -- run this directly
whenever `alembic upgrade head` (or the API server) fails to connect, before
re-running the real thing. Bypasses Alembic/SQLAlchemy entirely and talks to
Postgres with a bare asyncpg connection, so a failure here isolates the
problem to "the database is unreachable" rather than "something in this
project's migration/engine wiring is broken."

Usage:
    python scripts/diagnose_db_connection.py

On Windows, if the connection attempt below just hangs and times out (no
immediate "connection refused"), that is consistent with a known asyncpg +
Windows `ProactorEventLoop` SSL-handshake issue -- this script forces
`WindowsSelectorEventLoopPolicy` first specifically to rule that out. If it
still times out after that, the problem is almost certainly network-level
(Neon compute suspended/cold-starting, a firewall silently dropping
outbound port 5432, or a stale/incorrect DATABASE_URL) rather than anything
in this codebase.
"""

from __future__ import annotations

import asyncio
import sys
import time
from urllib.parse import urlsplit

import asyncpg

from app.shared.config.settings import get_settings

# See this module's docstring -- asyncpg's SSL handshake has known issues
# under Windows' default ProactorEventLoop in some environments. Forcing the
# selector-based policy is a no-op on non-Windows platforms.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

CONNECT_TIMEOUT_SECONDS = 30


def _to_asyncpg_dsn(raw_url: str) -> str:
    """asyncpg's `connect()` doesn't understand the `+asyncpg` SQLAlchemy
    dialect suffix -- strip it, same normalization idea as
    `app.database.session._normalize_database_url`, just for a bare
    `asyncpg.connect()` call instead of a SQLAlchemy engine.
    """
    parsed = urlsplit(raw_url)
    scheme = parsed.scheme.replace("+asyncpg", "")
    return raw_url.replace(parsed.scheme, scheme, 1)


async def main() -> None:
    raw_url = str(get_settings().database_url)
    dsn = _to_asyncpg_dsn(raw_url)
    parsed = urlsplit(dsn)

    print(f"Target host: {parsed.hostname}")
    print(f"Target port: {parsed.port or 5432}")
    print(f"Database:    {parsed.path.lstrip('/')}")
    print(f"SSL:         forced on (matches app.database.session._build_engine)")
    print(f"Timeout:     {CONNECT_TIMEOUT_SECONDS}s")
    print("Connecting...")

    start = time.monotonic()
    try:
        conn = await asyncpg.connect(dsn, ssl=True, timeout=CONNECT_TIMEOUT_SECONDS)
    except TimeoutError:
        elapsed = time.monotonic() - start
        print(f"\nTIMED OUT after {elapsed:.1f}s.")
        print(
            "This means the TCP/SSL handshake never completed -- not that "
            "Postgres rejected the credentials. Most likely causes:\n"
            "  1. Neon's compute is suspended and didn't wake up in time -- "
            "wait ~30s and retry once (cold starts can take longer than "
            "this timeout the very first time).\n"
            "  2. A firewall/VPN is silently dropping outbound traffic to "
            f"{parsed.hostname}:{parsed.port or 5432} rather than rejecting "
            "it outright (a rejection would fail fast, not time out).\n"
            "  3. DATABASE_URL in .env is stale -- confirm the host/port/"
            "credentials still match what's shown in the Neon dashboard.\n"
        )
        raise SystemExit(1)
    except Exception as exc:
        elapsed = time.monotonic() - start
        print(f"\nFAILED after {elapsed:.1f}s: {type(exc).__name__}: {exc}")
        print(
            "This failed fast rather than timing out -- likely a credential, "
            "database-name, or SSL-mode mismatch, not a network/firewall issue."
        )
        raise SystemExit(1)

    elapsed = time.monotonic() - start
    version = await conn.fetchval("SELECT version()")
    await conn.close()
    print(f"\nConnected successfully in {elapsed:.1f}s.")
    print(f"Server: {version}")
    print("\nDATABASE_URL is reachable -- if `alembic upgrade head` still "
          "fails, the problem is in the migration/engine wiring, not "
          "connectivity, and the error from that command is now the one "
          "to chase.")


if __name__ == "__main__":
    asyncio.run(main())
