"""Tests for `app.api.errors.ekip_error_handler` -- registered against a
throwaway FastAPI app (not `app.api.main.app`) so this exercises only the
error-mapping behavior, not the real routers' dependencies.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.errors import ekip_error_handler
from app.core.exceptions import ConflictError, EKIPError, NotFoundError, ValidationError


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(EKIPError, ekip_error_handler)

    @app.get("/not-found")
    def _raise_not_found() -> None:
        raise NotFoundError("Incident not found.", error_code="incident.not_found", detail={"id": "1"})

    @app.get("/conflict")
    def _raise_conflict() -> None:
        raise ConflictError("Already exists.")

    @app.get("/validation")
    def _raise_validation() -> None:
        raise ValidationError("Bad input.")

    return app


def test_not_found_maps_to_404_with_error_body() -> None:
    client = TestClient(_make_app())
    response = client.get("/not-found")

    assert response.status_code == 404
    assert response.json() == {
        "error_code": "incident.not_found",
        "message": "Incident not found.",
        "detail": {"id": "1"},
    }


def test_conflict_maps_to_409_with_default_error_code() -> None:
    client = TestClient(_make_app())
    response = client.get("/conflict")

    assert response.status_code == 409
    assert response.json()["error_code"] == "conflict"


def test_validation_maps_to_400() -> None:
    client = TestClient(_make_app())
    response = client.get("/validation")

    assert response.status_code == 400
