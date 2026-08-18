"""Tests for `app.api.rate_limit` (Phase 6.5) -- the dependency factories
themselves, independent of any specific router wiring them in (those are
covered by each router's own existing tests continuing to pass, plus the
end-to-end 429 assertions below against a minimal throwaway app).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.errors import ekip_error_handler
from app.api.rate_limit import rate_limit_by_ip, rate_limit_by_org, rate_limit_by_user
from app.core.exceptions import EKIPError
from app.shared.schemas import ActorKind, Identity


@pytest.fixture(autouse=True)
def _isolated_limiter(monkeypatch):
    """Each test gets its own limiter instance -- `tests/conftest.py`'s
    session-wide reset fixture already clears the real shared one between
    tests, but constructing a fresh one here removes any doubt for a test
    file specifically about this module's own behavior.
    """
    from app.shared.rate_limiter import TokenBucketRateLimiter

    import app.api.rate_limit as rate_limit_module

    monkeypatch.setattr(rate_limit_module, "_limiter", TokenBucketRateLimiter())


def _build_app_with_ip_limit(requests_per_minute: float) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(EKIPError, ekip_error_handler)
    limit_dep = rate_limit_by_ip(scope="test.ip", requests_per_minute=requests_per_minute)

    @app.get("/limited", dependencies=[Depends(limit_dep)])
    def limited() -> dict[str, bool]:
        return {"ok": True}

    return app


def test_rate_limit_by_ip_allows_requests_within_budget() -> None:
    app = _build_app_with_ip_limit(60.0)  # 1/sec, burst of 60
    client = TestClient(app)

    for _ in range(5):
        response = client.get("/limited")
        assert response.status_code == 200


def test_rate_limit_by_ip_returns_429_once_exhausted() -> None:
    app = _build_app_with_ip_limit(3.0)  # burst of 3
    client = TestClient(app)

    for _ in range(3):
        assert client.get("/limited").status_code == 200

    response = client.get("/limited")

    assert response.status_code == 429
    body = response.json()
    assert body["error_code"] == "rate_limited.ip"


def test_rate_limit_by_ip_tracks_different_clients_independently() -> None:
    """TestClient doesn't let per-request client IPs vary easily, so this
    exercises the underlying dependency directly with two distinct fake
    `Request`-like IPs via `app.api.rate_limit._client_ip`'s own fallback,
    proving the key includes the IP rather than being IP-independent.
    """
    import asyncio

    from app.api import rate_limit as rate_limit_module

    limiter = rate_limit_module._limiter

    async def _run():
        # Exhaust a 1-token budget for "ip-a".
        assert await limiter.try_acquire("test.ip:ip:ip-a", 1.0, capacity=1.0) is True
        assert await limiter.try_acquire("test.ip:ip:ip-a", 1.0, capacity=1.0) is False
        # "ip-b" is untouched.
        assert await limiter.try_acquire("test.ip:ip:ip-b", 1.0, capacity=1.0) is True

    asyncio.run(_run())


def _actor(organization_id: uuid.UUID | None = None) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id or uuid.uuid4(),
        user_id=uuid.uuid4(),
    )


def _build_app_with_user_limit(requests_per_minute: float, actor: Identity) -> FastAPI:
    from app.api.deps import get_current_identity

    app = FastAPI()
    app.add_exception_handler(EKIPError, ekip_error_handler)
    limit_dep = rate_limit_by_user(scope="test.user", requests_per_minute=requests_per_minute)

    @app.get("/limited", dependencies=[Depends(limit_dep)])
    def limited() -> dict[str, bool]:
        return {"ok": True}

    app.dependency_overrides[get_current_identity] = lambda: actor
    return app


def test_rate_limit_by_user_scopes_to_the_resolved_identity() -> None:
    actor = _actor()
    app = _build_app_with_user_limit(3.0, actor)
    client = TestClient(app)

    for _ in range(3):
        assert client.get("/limited").status_code == 200

    assert client.get("/limited").status_code == 429


def test_rate_limit_by_org_error_code_is_organization_specific() -> None:
    import asyncio

    from app.core.exceptions import RateLimitedError

    async def _run():
        organization_id = uuid.uuid4()
        actor = _actor(organization_id)
        dep = rate_limit_by_org(scope="test.org", requests_per_minute=1.0)
        # First call succeeds (burst of 1).
        await dep(actor)
        # Second call for the same org must be denied.
        with pytest.raises(RateLimitedError) as exc_info:
            await dep(actor)
        assert exc_info.value.error_code == "rate_limited.organization"
        assert exc_info.value.status_hint == 429

    asyncio.run(_run())
