"""SSRF guard and issuer normalization (`app.shared.outbound_url`)."""

from __future__ import annotations

import pytest

from app.shared import outbound_url
from app.shared.outbound_url import (
    UnsafeOutboundUrlError,
    assert_public_https_url,
    issuers_equal,
    normalize_issuer,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/x",  # not https
        "ftp://example.com",
        "file:///etc/passwd",
        "https://",  # no host
        "https://user:pw@example.com",  # credentials
        "https://127.0.0.1/",
        "https://127.1.2.3/",
        "https://10.0.0.1/",
        "https://172.16.5.4/",
        "https://192.168.1.1/",
        "https://169.254.169.254/latest/meta-data",  # cloud metadata
        "https://0.0.0.0/",
        "https://[::1]/",
        "https://[fe80::1]/",
        "https://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
        "https://localhost/",
    ],
)
async def test_unsafe_urls_are_rejected(url) -> None:
    with pytest.raises(UnsafeOutboundUrlError):
        await assert_public_https_url(url)


@pytest.mark.asyncio
async def test_a_public_literal_address_is_accepted() -> None:
    await assert_public_https_url("https://8.8.8.8/.well-known/openid-configuration")


@pytest.mark.asyncio
async def test_hostname_resolving_to_any_private_address_is_rejected(monkeypatch) -> None:
    async def fake_getaddrinfo(self, host, port, **kw):
        return [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("10.1.2.3", 0)),  # one bad record is enough (DNS rebinding style)
        ]

    import asyncio

    monkeypatch.setattr(asyncio.get_running_loop().__class__, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeOutboundUrlError):
        await assert_public_https_url("https://idp.example.com")


@pytest.mark.asyncio
async def test_unresolvable_host_is_rejected(monkeypatch) -> None:
    async def fail(self, host, port, **kw):
        raise OSError("no such host")

    import asyncio

    monkeypatch.setattr(asyncio.get_running_loop().__class__, "getaddrinfo", fail)
    with pytest.raises(UnsafeOutboundUrlError):
        await assert_public_https_url("https://does-not-exist.example")


@pytest.mark.asyncio
async def test_insecure_development_switch_allows_local_http_but_not_garbage() -> None:
    await assert_public_https_url("http://127.0.0.1:8080/x", allow_insecure=True)
    with pytest.raises(UnsafeOutboundUrlError):
        await assert_public_https_url("javascript:alert(1)", allow_insecure=True)
    with pytest.raises(UnsafeOutboundUrlError):
        await assert_public_https_url("http://user:pw@127.0.0.1", allow_insecure=True)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Acme.Okta.com", "https://acme.okta.com"),
        ("https://acme.okta.com/", "https://acme.okta.com"),
        ("  https://acme.okta.com/oauth2/default/  ", "https://acme.okta.com/oauth2/default"),
        ("HTTPS://tenant.auth0.com/", "https://tenant.auth0.com"),
        ("https://login.microsoftonline.com/TENANT-ID/v2.0", "https://login.microsoftonline.com/TENANT-ID/v2.0"),
        ("https://accounts.google.com", "https://accounts.google.com"),
        ("https://idp.example.com:8443/realm/", "https://idp.example.com:8443/realm"),
    ],
)
def test_normalize_issuer(raw, expected) -> None:
    assert normalize_issuer(raw) == expected


@pytest.mark.parametrize(
    "bad",
    ["", "acme.okta.com", "http://acme.okta.com", "https://acme.okta.com?x=1", "https://acme.okta.com#f", "https://u:p@acme.okta.com"],
)
def test_normalize_issuer_rejects_unacceptable_values(bad) -> None:
    with pytest.raises(UnsafeOutboundUrlError):
        normalize_issuer(bad)


def test_issuers_equal_ignores_trailing_slash_and_case_but_nothing_else() -> None:
    assert issuers_equal("https://t.auth0.com/", "https://t.auth0.com")
    assert issuers_equal("https://T.auth0.com", "https://t.auth0.com/")
    assert not issuers_equal("https://t.auth0.com/a", "https://t.auth0.com/b")
    assert not issuers_equal("https://t.auth0.com", "http://t.auth0.com")
    assert not issuers_equal(None, "https://t.auth0.com")
    assert not issuers_equal(["https://t.auth0.com"], "https://t.auth0.com")
    assert not issuers_equal("not a url", "not a url")
    assert outbound_url.issuers_equal("http://127.0.0.1:1", "http://127.0.0.1:1/", allow_insecure=True)
