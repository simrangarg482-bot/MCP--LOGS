"""EKIP MCP server process entrypoint."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from mcp.server.transport_security import TransportSecuritySettings

from app.database.session import session_scope, set_tenant_context
from app.mcp.servers import main as mcp_assembly
from app.mcp.servers import server as server_module
from app.shared.config.logging import configure_logging, get_logger
from app.shared.config.settings import get_settings

configure_logging()
logger = get_logger(__name__)

# Trigger registration of all tools, resources, and prompts.
_ = mcp_assembly.mcp_server

# Inject database dependencies.
server_module.session_factory = session_scope
server_module.set_tenant_context = set_tenant_context

# Bind every interface by default: a container platform (Railway, Fly, Cloud
# Run) routes to the service over its own network, never over loopback.
# `MCP_HOST` can override it back to `127.0.0.1` for a local-only run.
_DEFAULT_HOST = "0.0.0.0"


def _host_forms(hostname: str) -> list[str]:
    """Both Host-header spellings the transport layer must accept for one
    hostname: the bare host (a request whose port is the URL-scheme default
    and therefore omitted from `Host`, e.g. a public `:443` endpoint) and the
    `:*` wildcard (`mcp.server.transport_security` matches any explicit port
    against it).
    """
    hostname = hostname.strip().strip("/").lower()
    if not hostname:
        return []
    return [hostname, f"{hostname}:*"]


def _hostname_of(value: str) -> str:
    """The hostname inside a base URL or a bare `host[:port]` string."""
    value = value.strip()
    if not value:
        return ""
    split = urlsplit(value if "//" in value else f"//{value}")
    return split.hostname or ""


def build_allowed_hosts() -> list[str]:
    """Assemble `TransportSecuritySettings.allowed_hosts` from the
    environment rather than a hardcoded list.

    `mcp.server.transport_security` rejects any request whose `Host` header
    is not listed here with `421 Invalid Host header`, before MCP dispatch
    ever runs -- so every hostname this server is actually reached at
    (loopback for local/health-probe traffic, the public domain Claude
    connects to) has to be represented.
    """
    hosts: list[str] = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]

    # The OAuth issuer/resource URL Claude discovers this server at -- always
    # a hostname it will send us as `Host`.
    hosts += _host_forms(_hostname_of(get_settings().mcp_public_base_url))

    # Railway injects these for any service that has a domain attached, so a
    # Railway deployment needs no extra manual variable for the common case.
    for env_name in ("RAILWAY_PUBLIC_DOMAIN", "RAILWAY_PRIVATE_DOMAIN"):
        hosts += _host_forms(os.environ.get(env_name, ""))

    # Explicit escape hatch for anything the two sources above miss (a custom
    # domain, a tunnel hostname): comma-separated, host or host:port each.
    for extra in os.environ.get("MCP_ALLOWED_HOSTS", "").split(","):
        hosts += _host_forms(_hostname_of(extra))

    seen: set[str] = set()
    return [host for host in hosts if not (host in seen or seen.add(host))]


def resolve_port() -> int:
    """`$PORT` (injected by every container platform) wins; fall back to
    `Settings.mcp_port` (`MCP_PORT`) for a local run.
    """
    return int(os.environ.get("PORT") or get_settings().mcp_port)


if __name__ == "__main__":
    # `app.mcp.servers.server`'s OAuth wiring (`AuthSettings.issuer_url`/
    # `resource_server_url`, for Claude's remote-connector OAuth flow) reads
    # `Settings.mcp_public_base_url` (`MCP_PUBLIC_BASE_URL`) -- keep it pointed
    # at the SAME public hostname this server is reachable at, or OAuth
    # discovery will advertise a URL Claude can't reach. That hostname is
    # picked up into `allowed_hosts` automatically by `build_allowed_hosts`.
    host = os.environ.get("MCP_HOST", _DEFAULT_HOST)
    port = resolve_port()
    allowed_hosts = build_allowed_hosts()
    transport_security = TransportSecuritySettings(allowed_hosts=allowed_hosts)

    logger.info(
        "mcp_server_starting",
        host=host,
        port=port,
        allowed_hosts=allowed_hosts,
        public_base_url=get_settings().mcp_public_base_url,
    )
    server_module.mcp_server.run(
        transport="streamable-http",
        host=host,
        port=port,
        transport_security=transport_security,
    )
