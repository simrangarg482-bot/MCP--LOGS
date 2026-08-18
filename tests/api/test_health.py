"""Tests for `app.api.routers.health` -- `/health` (liveness) and `/ready`
(readiness), added in the Phase 3 production-readiness pass.

Both are deliberately unauthenticated -- no `dependency_overrides` for
`get_current_identity` needed here, unlike every other router's tests.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import main as api_main
from app.api.routers import health as health_router


@pytest.fixture()
def client():
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def test_health_returns_ok_with_no_dependency_checks(client, monkeypatch) -> None:
    """Liveness never touches the database or Redis -- monkeypatch both to
    raise, and confirm `/health` is completely unaffected.
    """

    async def explode(*args, **kwargs):
        raise AssertionError("`/health` must never call this")

    monkeypatch.setattr(health_router, "_check_database", explode)
    monkeypatch.setattr(health_router, "_check_redis", explode)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_ready_reports_ok_when_database_reachable(monkeypatch) -> None:
    async def fake_check_database():
        return health_router.ReadinessDependency(status="ok")

    monkeypatch.setattr(health_router, "_check_database", fake_check_database)

    result = await health_router._check_database()
    assert result.status == "ok"


def test_ready_returns_503_when_database_unreachable(client, monkeypatch) -> None:
    async def failing_check_database():
        return health_router.ReadinessDependency(status="unavailable", detail="database unreachable")

    monkeypatch.setattr(health_router, "_check_database", failing_check_database)

    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["database"]["status"] == "unavailable"


def test_ready_returns_200_when_database_ok_even_if_redis_degraded(client, monkeypatch) -> None:
    """Regression guard for the explicit "Redis unavailable must not make the
    whole API unavailable" requirement: readiness must stay 200/ready with
    Redis reported as merely `degraded`, never failing the whole probe over
    a dependency most traffic doesn't touch (matches `_lifespan`'s own
    already-established degraded-Redis behavior).
    """

    async def ok_database():
        return health_router.ReadinessDependency(status="ok")

    monkeypatch.setattr(health_router, "_check_database", ok_database)
    # No app.state.arq_pool set on the TestClient's app -- `_check_redis`
    # takes the `arq_pool is None` branch, exactly like a real Redis-outage
    # startup.

    response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["redis"]["status"] == "degraded"


def test_ready_reports_redis_ok_when_arq_pool_pings_successfully(client, monkeypatch) -> None:
    class _FakeArqPool:
        async def ping(self) -> bool:
            return True

    async def ok_database():
        return health_router.ReadinessDependency(status="ok")

    monkeypatch.setattr(health_router, "_check_database", ok_database)
    api_main.app.state.arq_pool = _FakeArqPool()
    try:
        response = client.get("/ready")
    finally:
        api_main.app.state.arq_pool = None

    assert response.status_code == 200
    assert response.json()["redis"]["status"] == "ok"


def test_health_and_ready_require_no_authentication(client) -> None:
    """No Authorization header supplied at all -- both must still respond
    (never 401/403), unlike every other real endpoint in this API.
    """
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code in (200, 503)
