"""Tests for `app.api.middleware.RequestContextMiddleware` (Phase 5.2)."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.api import main as api_main


def test_response_echoes_a_generated_request_id_when_none_supplied() -> None:
    client = TestClient(api_main.app)

    response = client.get("/health")

    assert "X-Request-ID" in response.headers
    # Must be a real, well-formed id -- not an empty string or a static
    # placeholder value.
    uuid.UUID(response.headers["X-Request-ID"])


def test_response_echoes_back_a_caller_supplied_request_id() -> None:
    client = TestClient(api_main.app)
    supplied_id = "caller-supplied-request-id-123"

    response = client.get("/health", headers={"X-Request-ID": supplied_id})

    assert response.headers["X-Request-ID"] == supplied_id


def test_two_requests_get_two_different_generated_ids() -> None:
    client = TestClient(api_main.app)

    first = client.get("/health")
    second = client.get("/health")

    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]


def test_request_id_present_even_on_error_response() -> None:
    client = TestClient(api_main.app)

    response = client.get("/incidents/not-a-valid-uuid")

    assert "X-Request-ID" in response.headers
