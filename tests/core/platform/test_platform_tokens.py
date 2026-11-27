"""Platform token design: separate audience, no cross-use, kill switch, production refusal -- no database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from jose import jwt as jose_jwt

from app.core.auth import service as auth_service
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.platform import auth as platform_auth
from app.shared.config.settings import Settings, get_settings

SECRET = "unit-test-only-signing-key-with-32-bytes-minimum"


def _settings(**over):
    base = dict(
        jwt_secret_key=SECRET,
        jwt_algorithm="HS256",
        jwt_expiry_minutes=60,
        platform_admin_enabled=True,
        platform_token_expiry_minutes=15,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(platform_auth, "get_settings", _settings)
    monkeypatch.setattr(auth_service, "get_settings", _settings)


def test_platform_token_shape_default_lifetime_and_no_refresh() -> None:
    uid = uuid.uuid4()
    token, issued, expires = platform_auth.issue_platform_token(uid)
    claims = jose_jwt.decode(token, SECRET, algorithms=["HS256"], audience="ekip-platform")
    assert claims["type"] == "platform" and claims["aud"] == claims["iss"] == "ekip-platform"
    assert "organization_id" not in claims and "oe" not in claims
    assert expires - issued == timedelta(minutes=15)
    verified = platform_auth.verify_platform_token(token)
    assert verified.user_id == uid


def test_lifetime_follows_the_setting(monkeypatch) -> None:
    monkeypatch.setattr(platform_auth, "get_settings", lambda: _settings(platform_token_expiry_minutes=5))
    _t, issued, expires = platform_auth.issue_platform_token(uuid.uuid4())
    assert expires - issued == timedelta(minutes=5)


def test_a_platform_token_is_rejected_by_the_tenant_verifier() -> None:
    token, _i, _e = platform_auth.issue_platform_token(uuid.uuid4())
    with pytest.raises(PermissionDeniedError) as info:
        auth_service.verify_access_token(token)
    assert info.value.error_code == "auth.invalid_token"


def test_a_platform_token_is_rejected_by_the_mcp_verifier() -> None:
    token, _i, _e = platform_auth.issue_platform_token(uuid.uuid4())
    with pytest.raises(PermissionDeniedError):
        auth_service.verify_access_token(token, audience="https://mcp.example/mcp", issuer="https://mcp.example/mcp")


def test_a_tenant_token_is_rejected_by_the_platform_verifier() -> None:
    token, _i, _e = auth_service._issue_access_token(uuid.uuid4(), uuid.uuid4())
    with pytest.raises(PermissionDeniedError) as info:
        platform_auth.verify_platform_token(token)
    assert info.value.error_code == "platform.invalid_token"


def test_an_mcp_token_is_rejected_by_the_platform_verifier() -> None:
    token, _i, _e = auth_service._issue_access_token(
        uuid.uuid4(), uuid.uuid4(), scope="mcp", audience="https://mcp.example/mcp", issuer="https://mcp.example/mcp"
    )
    with pytest.raises(PermissionDeniedError):
        platform_auth.verify_platform_token(token)


def test_an_org_selection_style_or_untyped_token_is_rejected() -> None:
    now = datetime.now(UTC)
    for claims in (
        {"sub": str(uuid.uuid4()), "aud": "ekip-platform", "iss": "ekip-platform", "iat": int(now.timestamp()), "exp": int((now + timedelta(minutes=5)).timestamp())},  # no type
        {"sub": str(uuid.uuid4()), "type": "access", "aud": "ekip-platform", "iss": "ekip-platform", "iat": int(now.timestamp()), "exp": int((now + timedelta(minutes=5)).timestamp())},
        {"sub": str(uuid.uuid4()), "type": "platform", "aud": "other", "iss": "ekip-platform", "iat": int(now.timestamp()), "exp": int((now + timedelta(minutes=5)).timestamp())},
        {"sub": str(uuid.uuid4()), "type": "platform", "aud": "ekip-platform", "iss": "elsewhere", "iat": int(now.timestamp()), "exp": int((now + timedelta(minutes=5)).timestamp())},
        {"sub": "not-a-uuid", "type": "platform", "aud": "ekip-platform", "iss": "ekip-platform", "iat": int(now.timestamp()), "exp": int((now + timedelta(minutes=5)).timestamp())},
    ):
        token = jose_jwt.encode(claims, SECRET, algorithm="HS256")
        with pytest.raises(PermissionDeniedError):
            platform_auth.verify_platform_token(token)


def test_expired_and_badly_signed_platform_tokens_are_rejected() -> None:
    now = datetime.now(UTC)
    base = {"sub": str(uuid.uuid4()), "type": "platform", "aud": "ekip-platform", "iss": "ekip-platform", "iat": int(now.timestamp())}
    expired = jose_jwt.encode({**base, "exp": int((now - timedelta(seconds=1)).timestamp())}, SECRET, algorithm="HS256")
    forged = jose_jwt.encode({**base, "exp": int((now + timedelta(minutes=5)).timestamp())}, "another-secret-of-sufficient-length-xx", algorithm="HS256")
    for token in (expired, forged, "garbage"):
        with pytest.raises(PermissionDeniedError):
            platform_auth.verify_platform_token(token)


def test_the_kill_switch_refuses_everything_first(monkeypatch) -> None:
    monkeypatch.setattr(platform_auth, "get_settings", lambda: _settings(platform_admin_enabled=False))
    assert platform_auth.platform_access_available() is False
    with pytest.raises(NotFoundError) as info:
        platform_auth.assert_platform_enabled()
    assert info.value.error_code == "platform.unavailable"


@pytest.mark.asyncio
async def test_authenticate_checks_the_kill_switch_before_looking_at_the_token(monkeypatch) -> None:
    monkeypatch.setattr(platform_auth, "get_settings", lambda: _settings(platform_admin_enabled=False))
    with pytest.raises(NotFoundError):
        await platform_auth.authenticate(None, "anything")  # type: ignore[arg-type]
    with pytest.raises(NotFoundError):
        from app.core.platform.schemas import PlatformLoginRequest

        await platform_auth.login(None, PlatformLoginRequest(email="a@b.c", password="x"))  # type: ignore[arg-type]


def test_the_default_is_disabled() -> None:
    assert Settings.model_fields["platform_admin_enabled"].default is False
    assert Settings.model_fields["platform_token_expiry_minutes"].default == 15


def test_production_refuses_to_boot_with_platform_access_enabled() -> None:
    values = get_settings().model_dump()
    values.update(environment="production", platform_admin_enabled=True)
    with pytest.raises(ValueError, match="PLATFORM_ADMIN_ENABLED"):
        Settings.model_validate(values)
