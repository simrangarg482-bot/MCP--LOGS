"""Tests for the real CORS behavior `app.api.main.create_app` configures --
not against the shared `api_main.app` singleton (whose `cors_allowed_origins`
is resolved once, at import time, from whatever environment happened to be
present, and can't be reconfigured per test), but a small, isolated FastAPI
app built with the exact same `CORSMiddleware` call (same
`allow_credentials=True`/`allow_methods=["*"]`/`allow_headers=["*"]`) against
a known, controlled origin list -- so these tests exercise the real
middleware's behavior, deterministically, regardless of the running
environment's actual `.env`.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

_ALLOWED_ORIGINS = ["https://app.example.com", "https://admin.example.com"]


def _build_test_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"status": "ok"}

    return app


def test_allowed_origin_receives_cors_headers() -> None:
    client = TestClient(_build_test_app())

    response = client.get("/ping", headers={"Origin": "https://app.example.com"})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://app.example.com"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_disallowed_origin_receives_no_cors_headers() -> None:
    client = TestClient(_build_test_app())

    response = client.get("/ping", headers={"Origin": "https://evil.example.com"})

    # Starlette still returns the response body (CORS is enforced by the
    # browser refusing to expose the response to script, not by the server
    # refusing to answer) -- what must NOT happen is an
    # `access-control-allow-origin` header naming the disallowed origin.
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_second_allowed_origin_in_a_multi_origin_list_also_receives_cors_headers() -> None:
    client = TestClient(_build_test_app())

    response = client.get("/ping", headers={"Origin": "https://admin.example.com"})

    assert response.headers["access-control-allow-origin"] == "https://admin.example.com"


def test_preflight_for_disallowed_origin_is_rejected() -> None:
    client = TestClient(_build_test_app())

    response = client.options(
        "/ping",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )

    # Starlette's CORSMiddleware answers a disallowed-origin preflight with
    # 400, not by silently omitting headers from an otherwise-200 response
    # (unlike the simple-request case above).
    assert response.status_code == 400
