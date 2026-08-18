"""Tests for `app.ingestion.url_safety.assert_safe_connector_url` -- the
SSRF guard added in the Phase 3 production-readiness pass for
`JiraConnector`/`ConfluenceConnector`'s tenant-admin-supplied `base_url`
(see that module's docstring for why only those two connectors need it).
"""

from __future__ import annotations

import socket

import pytest

from app.ingestion.url_safety import UnsafeConnectorUrlError, assert_safe_connector_url


def test_rejects_missing_hostname() -> None:
    with pytest.raises(UnsafeConnectorUrlError, match="no hostname"):
        assert_safe_connector_url("not-a-url")


def test_rejects_non_http_scheme() -> None:
    with pytest.raises(UnsafeConnectorUrlError, match="scheme must be"):
        assert_safe_connector_url("ftp://example.com")


def test_rejects_localhost() -> None:
    with pytest.raises(UnsafeConnectorUrlError, match="disallowed address"):
        assert_safe_connector_url("http://localhost:8000")


def test_rejects_loopback_ip_literal() -> None:
    with pytest.raises(UnsafeConnectorUrlError, match="disallowed address"):
        assert_safe_connector_url("http://127.0.0.1/api")


def test_rejects_cloud_metadata_endpoint() -> None:
    with pytest.raises(UnsafeConnectorUrlError, match="disallowed address"):
        assert_safe_connector_url("http://169.254.169.254/latest/meta-data/")


def test_rejects_private_rfc1918_address() -> None:
    with pytest.raises(UnsafeConnectorUrlError, match="disallowed address"):
        assert_safe_connector_url("https://10.0.0.5/wiki")
    with pytest.raises(UnsafeConnectorUrlError, match="disallowed address"):
        assert_safe_connector_url("https://192.168.1.1/wiki")


def test_rejects_unresolvable_hostname() -> None:
    with pytest.raises(UnsafeConnectorUrlError, match="could not be resolved"):
        assert_safe_connector_url("https://this-host-does-not-exist.invalid/")


def test_allows_a_real_external_https_url(monkeypatch) -> None:
    """A real public address (a literal IP, so this test has no real DNS
    dependency) must pass -- otherwise every legitimate Jira/Confluence
    Cloud instance would be rejected too.
    """
    # 93.184.216.34 was example.com's long-standing public IP; using
    # getaddrinfo's real resolution behavior via a fake avoids any actual
    # network dependency in this unit test.
    def fake_getaddrinfo(host, port, family=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert_safe_connector_url("https://acme.atlassian.net")
