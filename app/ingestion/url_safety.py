"""SSRF guard for connector base URLs that are tenant-admin-supplied rather
than hardcoded (currently `JiraConnector`/`ConfluenceConnector`'s
`config.config["base_url"]` -- unlike Slack/GitHub/Teams/SharePoint/Azure
DevOps, which only ever call a fixed, hardcoded public API host, so there is
no attacker-controlled destination for those connectors to validate).

`register_connector` (`core.tenancy.service`) only requires org-scoped
`tenancy:manage`, not platform-admin trust -- in a multi-tenant deployment,
any org admin can set `base_url` to anything, and the ingestion worker would
otherwise happily make outbound HTTP requests to it on every sync, including
internal services and cloud metadata endpoints (`169.254.169.254`) reachable
from wherever the worker actually runs. This module rejects the obvious
cases (non-http(s) scheme, and a resolved IP in a private/loopback/
link-local/reserved range) at connector-authentication time, before any
request to `base_url` is ever made.

Known, disclosed limitation: this validates the IP(s) a hostname resolves to
*once*, at `authenticate()` time -- it does not pin that IP for every
subsequent request on the same `httpx.AsyncClient`, so a DNS-rebinding
attacker who controls both the hostname's initial and later resolution could
still redirect later requests on that same client to a private address after
this check passes. Closing that fully would require a custom `httpx`
transport that resolves and re-validates per-connection, out of scope for
this pass; flagged here rather than silently left unstated.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_ALLOWED_SCHEMES = ("http", "https")


class UnsafeConnectorUrlError(RuntimeError):
    """Raised when a tenant-supplied connector `base_url` is rejected as an
    SSRF risk -- callers should treat this the same as any other
    connector-configuration error (surfaces as an ingestion `failed` run,
    not a crash).
    """


def _is_unsafe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def assert_safe_connector_url(base_url: str) -> None:
    """Raise `UnsafeConnectorUrlError` if `base_url` is not a safe,
    external HTTP(S) destination for the ingestion worker to call.

    Checks (in order): a parseable URL with a hostname, an http/https
    scheme, and every IP address the hostname resolves to being a real,
    external address -- not a private (RFC1918), loopback, link-local
    (which includes the `169.254.169.254` cloud metadata endpoint),
    reserved, multicast, or unspecified one.
    """
    parsed = urlparse(base_url)
    if not parsed.hostname:
        raise UnsafeConnectorUrlError(f"Connector base_url has no hostname: {base_url!r}")
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise UnsafeConnectorUrlError(
            f"Connector base_url scheme must be http or https, got {parsed.scheme!r}: {base_url!r}"
        )

    try:
        # AF_UNSPEC resolves both A and AAAA records, matching what a real
        # outbound request from this connector could actually connect to.
        resolved = socket.getaddrinfo(parsed.hostname, None, family=socket.AF_UNSPEC)
    except OSError as exc:
        raise UnsafeConnectorUrlError(
            f"Connector base_url hostname could not be resolved: {parsed.hostname!r} ({exc})"
        ) from exc

    for _family, _type, _proto, _canonname, sockaddr in resolved:
        ip = ipaddress.ip_address(sockaddr[0])
        if _is_unsafe_ip(ip):
            raise UnsafeConnectorUrlError(
                f"Connector base_url {base_url!r} resolves to a disallowed address ({ip}) -- "
                "internal, loopback, link-local, and reserved addresses are never permitted."
            )
