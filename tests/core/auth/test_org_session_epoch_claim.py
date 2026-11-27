"""The organization session epoch (`oe`) claim and the lifecycle wrapper's error mapping -- no database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from jose import jwt as jose_jwt

from app.core.auth import service as auth_service
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.tenancy import lifecycle
from tests.core.auth.test_sso_sessions_provisioning import _JWT_SECRET, _settings


@pytest.fixture(autouse=True)
def _settings_patch(monkeypatch):
    monkeypatch.setattr(auth_service, "get_settings", _settings)


def _claims(token):
    return jose_jwt.decode(token, _JWT_SECRET, algorithms=["HS256"])


def test_new_tokens_carry_the_epoch_they_were_issued_under() -> None:
    uid, org = uuid.uuid4(), uuid.uuid4()
    default, _, _ = auth_service._issue_access_token(uid, org)
    assert _claims(default)["oe"] == 0
    token, _, _ = auth_service._issue_access_token(uid, org, org_session_epoch=7)
    assert _claims(token)["oe"] == 7
    assert auth_service.verify_access_token(token).org_session_epoch == 7


def test_a_token_without_the_claim_verifies_as_epoch_zero() -> None:
    now = datetime.now(UTC)
    legacy = jose_jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "organization_id": str(uuid.uuid4()),
            "type": "access",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        },
        _JWT_SECRET,
        algorithm="HS256",
    )
    assert auth_service.verify_access_token(legacy).org_session_epoch == 0


@pytest.mark.parametrize("value", ["1", -1, 1.5, True, [1], {"a": 1}])
def test_a_malformed_epoch_claim_is_refused_not_defaulted(value) -> None:
    now = datetime.now(UTC)
    token = jose_jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "organization_id": str(uuid.uuid4()),
            "type": "access",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "oe": value,
        },
        _JWT_SECRET,
        algorithm="HS256",
    )
    with pytest.raises(PermissionDeniedError) as info:
        auth_service.verify_access_token(token)
    assert info.value.error_code == "auth.invalid_token"


def test_mcp_tokens_carry_the_epoch_too() -> None:
    token, _, _ = auth_service._issue_access_token(
        uuid.uuid4(), uuid.uuid4(), scope="mcp", audience="a", issuer="i", org_session_epoch=3
    )
    assert auth_service.verify_access_token(token, audience="a", issuer="i").org_session_epoch == 3


# --- lifecycle wrapper: database error codes -> application errors --------------------------------------


class _Orig(Exception):
    def __init__(self, sqlstate):
        super().__init__("x")
        self.sqlstate = sqlstate


class _FailingSession:
    def __init__(self, sqlstate):
        self.sqlstate = sqlstate

    async def execute(self, *_a, **_k):
        from sqlalchemy.exc import DBAPIError

        raise DBAPIError("stmt", {}, _Orig(self.sqlstate))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sqlstate", "error", "code"),
    [
        ("EK001", ConflictError, "organization.invalid_transition"),
        ("EK002", ConflictError, "organization.already_in_status"),
        ("EK003", PermissionDeniedError, "platform.operator_required"),
        ("EK004", NotFoundError, "organization.not_found"),
        ("EK005", ValidationError, "organization.invalid_status_request"),
    ],
)
async def test_database_error_codes_become_application_errors(sqlstate, error, code) -> None:
    with pytest.raises(error) as info:
        await lifecycle.set_organization_status(
            _FailingSession(sqlstate),  # type: ignore[arg-type]
            operator_user_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            new_status="suspended",
            reason="r",
        )
    assert info.value.error_code == code


@pytest.mark.asyncio
async def test_an_unrelated_database_error_is_not_swallowed() -> None:
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        await lifecycle.set_organization_status(
            _FailingSession("23505"),  # type: ignore[arg-type]
            operator_user_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            new_status="suspended",
            reason="r",
        )


@pytest.mark.asyncio
async def test_a_successful_call_returns_the_change() -> None:
    hid = uuid.uuid4()

    class _Result:
        def one(self):
            return ("onboarding", "suspended", hid, 1)

    class _Session:
        async def execute(self, *_a, **_k):
            return _Result()

    change = await lifecycle.set_organization_status(
        _Session(),  # type: ignore[arg-type]
        operator_user_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        new_status="suspended",
        reason="r",
    )
    assert (change.previous_status, change.new_status, change.history_id, change.session_epoch) == (
        "onboarding",
        "suspended",
        hid,
        1,
    )
