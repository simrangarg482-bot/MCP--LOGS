"""Regression coverage for the MCP server entrypoint's Railway-readiness:
binds `$PORT`, and its transport `allowed_hosts` list is assembled from the
environment rather than a hardcoded ngrok hostname.
"""

from __future__ import annotations

from app.shared.config.settings import get_settings
from scripts import run_mcp_server


def test_host_forms_returns_bare_and_wildcard_spellings() -> None:
    assert run_mcp_server._host_forms("mcp.example.com") == [
        "mcp.example.com",
        "mcp.example.com:*",
    ]
    assert run_mcp_server._host_forms("  MCP.Example.com/ ") == [
        "mcp.example.com",
        "mcp.example.com:*",
    ]
    assert run_mcp_server._host_forms("") == []


def test_hostname_of_accepts_urls_and_bare_hosts() -> None:
    assert run_mcp_server._hostname_of("https://mcp.example.com") == "mcp.example.com"
    assert run_mcp_server._hostname_of("https://mcp.example.com:8443/mcp") == "mcp.example.com"
    assert run_mcp_server._hostname_of("mcp.example.com:8443") == "mcp.example.com"
    assert run_mcp_server._hostname_of("") == ""


def test_resolve_port_prefers_the_platform_injected_port(monkeypatch) -> None:
    monkeypatch.setenv("PORT", "4567")
    assert run_mcp_server.resolve_port() == 4567


def test_resolve_port_falls_back_to_mcp_port_setting(monkeypatch) -> None:
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setenv("MCP_PORT", "8123")
    get_settings.cache_clear()
    try:
        assert run_mcp_server.resolve_port() == 8123
    finally:
        get_settings.cache_clear()


def test_build_allowed_hosts_is_assembled_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "https://ekip-mcp.up.railway.app")
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "ekip-mcp.up.railway.app")
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.example.com, tunnel.example.dev")
    get_settings.cache_clear()
    try:
        hosts = run_mcp_server.build_allowed_hosts()
    finally:
        get_settings.cache_clear()

    for expected in (
        "localhost",
        "localhost:*",
        "127.0.0.1:*",
        "ekip-mcp.up.railway.app",
        "ekip-mcp.up.railway.app:*",
        "mcp.example.com",
        "mcp.example.com:*",
        "tunnel.example.dev",
    ):
        assert expected in hosts

    # The hostname that used to be hardcoded in this script is not baked in
    # any more -- it only appears when an env var actually names it.
    assert "relic-heaviness-handsfree.ngrok-free.dev" not in hosts
    # De-duplicated, order preserved.
    assert len(hosts) == len(dict.fromkeys(hosts))


def test_build_allowed_hosts_without_optional_env_still_covers_loopback(monkeypatch) -> None:
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    monkeypatch.delenv("RAILWAY_PRIVATE_DOMAIN", raising=False)
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "http://localhost:8001")
    get_settings.cache_clear()
    try:
        hosts = run_mcp_server.build_allowed_hosts()
    finally:
        get_settings.cache_clear()

    assert "localhost:*" in hosts
    assert "127.0.0.1:*" in hosts
