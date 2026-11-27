"""Server-side record of one single sign-on login in flight.

`begin_sso_login` creates a transaction binding together everything the
callback must later prove it is continuing:

    organization slug, redirect URI, `state`, OIDC `nonce`, PKCE `code_verifier`

and hands the browser only an opaque, random **handle**. The PKCE verifier and
the nonce never leave the server (an earlier design returned the verifier to the
browser, where anything able to read the page could use it). `complete_sso_login`
presents the handle, the record is fetched **and deleted in one atomic step**
(`GETDEL`), so a transaction can be used exactly once -- a replayed callback, a
retried code or a second browser tab finds nothing. It is consumed even when the
rest of the callback then fails: a failed attempt cannot be retried, the person
starts again.

The record is stored under `sha256(handle)`, so a dump of Redis does not contain
a usable handle. It expires after `Settings.sso_transaction_ttl_seconds`; the
timestamp is also stored and re-checked so expiry does not depend on Redis alone.

Redis being unavailable fails the login closed (`SsoTransactionUnavailableError`)
-- unlike the request rate limiter, which fails open, there is no safe degraded
mode for authentication state.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Protocol

_KEY_PREFIX = "ekip:sso:txn:"
_MAX_HANDLE_LENGTH = 128


class SsoTransactionUnavailableError(RuntimeError):
    """The transaction store could not be reached."""


@dataclass(frozen=True)
class SsoTransaction:
    org_slug: str
    redirect_uri: str
    state: str
    nonce: str
    code_verifier: str
    expires_at: float

    def __repr__(self) -> str:  # never print the verifier / nonce
        return f"SsoTransaction(org_slug={self.org_slug!r})"


class SsoTransactionStore(Protocol):
    async def put(self, key: str, value: str, ttl_seconds: int) -> None: ...

    async def take(self, key: str) -> str | None:
        """Return and delete the value in one atomic step, or `None`."""
        ...


class RedisSsoTransactionStore:
    def __init__(self, redis) -> None:  # redis.asyncio.Redis
        self._redis = redis

    async def put(self, key: str, value: str, ttl_seconds: int) -> None:
        try:
            await self._redis.set(key, value, ex=ttl_seconds)
        except Exception as exc:  # noqa: BLE001 - any client/network failure
            raise SsoTransactionUnavailableError("transaction store unavailable") from exc

    async def take(self, key: str) -> str | None:
        try:
            value = await self._redis.getdel(key)
        except Exception as exc:  # noqa: BLE001
            raise SsoTransactionUnavailableError("transaction store unavailable") from exc
        if value is None:
            return None
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)


_default_store: SsoTransactionStore | None = None


def get_transaction_store() -> SsoTransactionStore:
    """Process-wide Redis-backed store (lazy: no connection until first use)."""
    global _default_store
    if _default_store is None:
        from redis.asyncio import Redis

        from app.shared.config.settings import get_settings

        _default_store = RedisSsoTransactionStore(Redis.from_url(str(get_settings().redis_url)))
    return _default_store


def _key(handle: str) -> str:
    return _KEY_PREFIX + hashlib.sha256(handle.encode("utf-8")).hexdigest()


async def create_transaction(
    store: SsoTransactionStore,
    *,
    org_slug: str,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_verifier: str,
    ttl_seconds: int,
) -> str:
    """Persist a transaction and return its opaque handle."""
    handle = secrets.token_urlsafe(32)
    payload = json.dumps(
        {
            "org_slug": org_slug,
            "redirect_uri": redirect_uri,
            "state": state,
            "nonce": nonce,
            "code_verifier": code_verifier,
            "expires_at": time.time() + ttl_seconds,
        }
    )
    await store.put(_key(handle), payload, ttl_seconds)
    return handle


async def consume_transaction(store: SsoTransactionStore, handle: str) -> SsoTransaction | None:
    """Fetch and delete the transaction for `handle`. `None` when it is unknown,
    already used, expired or malformed -- the caller cannot tell which."""
    if not handle or len(handle) > _MAX_HANDLE_LENGTH:
        return None
    raw = await store.take(_key(handle))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        transaction = SsoTransaction(
            org_slug=str(data["org_slug"]),
            redirect_uri=str(data["redirect_uri"]),
            state=str(data["state"]),
            nonce=str(data["nonce"]),
            code_verifier=str(data["code_verifier"]),
            expires_at=float(data["expires_at"]),
        )
    except (ValueError, KeyError, TypeError):
        return None
    if transaction.expires_at <= time.time():
        return None
    return transaction
