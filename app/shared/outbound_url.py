"""Guard for outbound requests to URLs an organization administrator controls.

Single sign-on contacts an *issuer URL* (and the token / JWKS / authorization
endpoints its discovery document names) that a tenant admin typed in. Without a
check, any admin could point EKIP at an internal service or a cloud metadata
address and use the API server as a proxy (SSRF).

`assert_public_https_url` requires `https` and a destination whose every
resolved address is public. The development switch
`Settings.sso_allow_insecure_issuers` (refused in production) relaxes both so a
local Keycloak / mock provider on `http://127.0.0.1` can be used.

Known limitation (same as `app.ingestion.url_safety`): the address is validated
at resolution time and not pinned for the connection that follows, so DNS
rebinding between the check and the request is not closed here. Redirects are
never followed by callers, and every URL taken from a discovery document is
re-checked before it is used.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeOutboundUrlError(ValueError):
    """The URL is not an acceptable outbound destination."""


def _is_unsafe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def check_url_shape(url: str, *, allow_insecure: bool) -> str:
    """Validate scheme / host / credentials without touching the network and
    return the hostname."""
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise UnsafeOutboundUrlError("URL scheme must be https.")
    if parts.scheme == "http" and not allow_insecure:
        raise UnsafeOutboundUrlError("URL scheme must be https.")
    if not parts.hostname:
        raise UnsafeOutboundUrlError("URL has no host.")
    if parts.username or parts.password:
        raise UnsafeOutboundUrlError("URL must not contain credentials.")
    return parts.hostname


def normalize_issuer(url: str, *, allow_insecure: bool = False) -> str:
    """Canonical form of an OIDC issuer URL: trimmed, lower-case scheme and
    host, no query / fragment / credentials, no trailing slash.

    Providers disagree about a trailing slash (Auth0 issues `https://x/`, Okta
    `https://x`), so issuers are *compared* in this form -- the token's `iss`,
    the discovery document's `issuer` and the configured value are each
    normalized and must then be equal. Raises `UnsafeOutboundUrlError`.
    """
    candidate = (url or "").strip()
    parts = urlsplit(candidate)
    check_url_shape(candidate, allow_insecure=allow_insecure)
    if parts.query or parts.fragment:
        raise UnsafeOutboundUrlError("Issuer URL must not contain a query or fragment.")
    host = parts.hostname or ""
    netloc = host.lower() if ":" not in host else f"[{host.lower()}]"
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    path = parts.path.rstrip("/")
    return f"{parts.scheme.lower()}://{netloc}{path}"


def issuers_equal(left: object, right: object, *, allow_insecure: bool = False) -> bool:
    """True when two issuer strings are the same issuer after normalization.
    Anything that is not a valid issuer URL compares unequal."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return normalize_issuer(left, allow_insecure=allow_insecure) == normalize_issuer(
            right, allow_insecure=allow_insecure
        )
    except UnsafeOutboundUrlError:
        return False


async def assert_public_https_url(url: str, *, allow_insecure: bool = False) -> None:
    """Raise `UnsafeOutboundUrlError` unless `url` is https and every address
    it resolves to is public. With `allow_insecure` only the shape is checked."""
    hostname = check_url_shape(url, allow_insecure=allow_insecure)
    if allow_insecure:
        return

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_unsafe_ip(literal):
            raise UnsafeOutboundUrlError("URL points at a non-public address.")
        return

    try:
        loop = asyncio.get_running_loop()
        resolved = await loop.getaddrinfo(hostname, None, family=socket.AF_UNSPEC)
    except OSError as exc:
        raise UnsafeOutboundUrlError("URL host could not be resolved.") from exc
    if not resolved:
        raise UnsafeOutboundUrlError("URL host could not be resolved.")
    for _family, _type, _proto, _canon, sockaddr in resolved:
        if _is_unsafe_ip(ipaddress.ip_address(sockaddr[0])):
            raise UnsafeOutboundUrlError("URL points at a non-public address.")
