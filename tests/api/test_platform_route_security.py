"""Structural guards for the platform surface (no database): a future endpoint cannot be added to
`/platform/*` without authentication, and nothing outside it accepts a platform credential."""

from __future__ import annotations

import importlib
import pkgutil

from fastapi.routing import APIRoute

import app.api.routers as routers_package
from app.api import deps as tenant_deps
from app.api import platform_deps

# The only two platform routes that do not need an operator: the public availability probe, and the sign-in.
_PUBLIC_PLATFORM_ROUTES = {("GET", "/platform/status"), ("POST", "/platform/auth/login")}
_WRITE_VERBS = ("activate", "suspend", "reactivate")


def _dependency_calls(dependant) -> set:
    found = {dependant.call}
    for sub in dependant.dependencies:
        found |= _dependency_calls(sub)
    return found


def _all_routes() -> list[APIRoute]:
    """Every route of every router module (the application wraps included routers, so walk the modules)."""
    found: list[APIRoute] = []
    for module_info in pkgutil.iter_modules(routers_package.__path__):
        module = importlib.import_module(f"app.api.routers.{module_info.name}")
        for attr in ("router", "admin_router"):
            router = getattr(module, attr, None)
            if router is not None:
                found += [r for r in router.routes if isinstance(r, APIRoute)]
    return found


def _platform_routes():
    return [r for r in _all_routes() if r.path.startswith("/platform")]


def test_every_platform_route_requires_an_authenticated_operator_except_the_two_public_ones() -> None:
    routes = _platform_routes()
    assert routes, "the platform router is not mounted"
    for route in routes:
        for method in route.methods - {"HEAD", "OPTIONS"}:
            calls = _dependency_calls(route.dependant)
            if (method, route.path) in _PUBLIC_PLATFORM_ROUTES:
                assert platform_deps.get_current_operator not in calls
            else:
                assert platform_deps.get_current_operator in calls, f"{method} {route.path} has no operator check"


def test_every_state_changing_platform_route_requires_the_admin_role() -> None:
    state_changing = [r for r in _platform_routes() if "POST" in r.methods and r.path not in {"/platform/auth/login"}]
    assert {r.path.rsplit("/", 1)[-1] for r in state_changing} == set(_WRITE_VERBS)
    for route in state_changing:
        assert platform_deps.get_admin_operator in _dependency_calls(route.dependant), route.path


def test_platform_routes_never_depend_on_tenant_identity_and_tenant_routes_never_on_platform_auth() -> None:
    for route in _all_routes():
        calls = _dependency_calls(route.dependant)
        if route.path.startswith("/platform"):
            assert tenant_deps.get_current_identity not in calls, route.path
        else:
            assert platform_deps.get_current_operator not in calls, route.path


def test_platform_login_has_both_rate_limits_and_nothing_mutating_is_a_GET() -> None:
    login = next(r for r in _platform_routes() if r.path == "/platform/auth/login")
    assert len(login.dependencies) >= 1  # the per-address limiter (the per-account one runs in the handler)
    for route in _platform_routes():
        if route.methods & {"GET"}:
            assert not route.methods - {"GET", "HEAD"}, route.path


def test_no_route_exists_that_could_create_or_grant_an_operator() -> None:
    forbidden = [
        r.path
        for r in _all_routes()
        if ("operators" in r.path.lower() or "grant" in r.path.lower()) and r.methods & {"POST", "PUT", "PATCH", "DELETE"}
    ]
    assert forbidden == []  # operators come only from the owner-credential CLI


def test_the_tenant_organization_api_exposes_no_status_write() -> None:
    for route in _all_routes():
        if route.path.startswith("/platform"):
            continue
        if route.path == "/organizations/{organization_id}" and route.methods & {"PATCH", "PUT", "DELETE"}:
            raise AssertionError(f"unexpected organization write route {route.methods} {route.path}")
        assert not (route.path.endswith("/status") and route.methods & {"POST", "PUT", "PATCH"}), route.path


def test_the_application_mounts_the_platform_router() -> None:
    from app.api import main as api_main

    assert "/platform/status" in api_main.app.openapi()["paths"]
