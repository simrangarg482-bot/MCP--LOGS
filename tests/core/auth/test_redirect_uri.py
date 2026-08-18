"""Tests for `app.core.auth.service._assert_redirect_uri_allowed` and its
use in `begin_sso_login`/`complete_sso_login` -- a Phase 3
production-hardening addition (security audit finding: `redirect_uri` was
previously accepted from the caller with no server-side validation at all,
relying entirely on the IdP's own registered-redirect-URI check).
"""

from __future__ import annotations

import uuid

import pytest

from app.core.auth import service as auth_service
from app.core.auth.schemas import SSOCallbackRequest
from app.core.exceptions import NotFoundError, PermissionDeniedError


class _FakeSettings:
    def __init__(self, cors_allowed_origins: list[str]) -> None:
        self.cors_allowed_origins = cors_allowed_origins


@pytest.fixture()
def allowed_origins(monkeypatch) -> list[str]:
    origins = ["https://app.example.com", "http://localhost:5173"]
    monkeypatch.setattr(auth_service, "get_settings", lambda: _FakeSettings(origins))
    return origins


def test_assert_redirect_uri_allowed_accepts_a_trusted_origin(allowed_origins) -> None:
    # Should not raise -- path/query beyond the origin is irrelevant.
    auth_service._assert_redirect_uri_allowed("https://app.example.com/auth/callback?x=1")


def test_assert_redirect_uri_allowed_rejects_an_untrusted_origin(allowed_origins) -> None:
    with pytest.raises(PermissionDeniedError, match="not an allowed origin"):
        auth_service._assert_redirect_uri_allowed("https://evil.example.com/callback")


def test_assert_redirect_uri_allowed_is_scheme_sensitive(allowed_origins) -> None:
    """`http://app.example.com` (wrong scheme) must not match an
    `https://app.example.com` allowlist entry -- origin comparison is exact,
    not host-only.
    """
    with pytest.raises(PermissionDeniedError, match="not an allowed origin"):
        auth_service._assert_redirect_uri_allowed("http://app.example.com/callback")


@pytest.mark.asyncio
async def test_begin_sso_login_rejects_disallowed_redirect_uri_before_any_lookup(
    allowed_origins, monkeypatch
) -> None:
    """The redirect_uri check must run before `begin_sso_login` even looks
    up the organization's SSO config -- an attacker-supplied redirect_uri
    should never get far enough to trigger a real IdP-discovery call.
    """
    called = False

    async def fake_get_organization_sso_config(session, org_slug):
        nonlocal called
        called = True
        raise AssertionError("should never be reached")

    monkeypatch.setattr(
        auth_service.tenancy_service,
        "get_organization_sso_config",
        fake_get_organization_sso_config,
    )

    with pytest.raises(PermissionDeniedError, match="not an allowed origin"):
        await auth_service.begin_sso_login(
            None, "acme", redirect_uri="https://evil.example.com/callback"
        )
    assert called is False


@pytest.mark.asyncio
async def test_complete_sso_login_rejects_disallowed_redirect_uri_before_code_exchange(
    allowed_origins, monkeypatch
) -> None:
    called = False

    async def fake_get_organization_sso_config(session, org_slug):
        nonlocal called
        called = True
        raise AssertionError("should never be reached")

    monkeypatch.setattr(
        auth_service.tenancy_service,
        "get_organization_sso_config",
        fake_get_organization_sso_config,
    )

    data = SSOCallbackRequest(
        org_slug="acme", code="fake-code", code_verifier="fake-verifier", state="fake-state"
    )
    with pytest.raises(PermissionDeniedError, match="not an allowed origin"):
        await auth_service.complete_sso_login(
            None, data, redirect_uri="https://evil.example.com/callback"
        )
    assert called is False
