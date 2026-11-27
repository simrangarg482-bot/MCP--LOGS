"""F5: a refresh token belongs to the OAuth client it was issued to."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.auth import service as auth_service
from app.core.auth.schemas import RefreshRequest
from app.core.exceptions import PermissionDeniedError
from tests.mcp.oauth_harness import full_flow, refresh_with, registered_client


def _row_for(store, raw_token: str):
    return store.refresh_rows[hashlib.sha256(raw_token.encode()).hexdigest()]


def _revoke(client, info: dict, token: str):
    return client.post(
        "/revoke",
        data={
            "token": token,
            "client_id": info["client_id"],
            "client_secret": info["client_secret"],
        },
    )


def test_issued_refresh_token_is_bound_to_the_receiving_client(client, store):
    info, tokens = full_flow(client, store)
    assert _row_for(store, tokens["refresh_token"]).oauth_client_id == info["client_id"]


def test_another_registered_client_cannot_refresh_it_and_the_token_survives(client, store):
    owner, tokens = full_flow(client, store)
    thief = registered_client(client)

    stolen = refresh_with(client, thief, tokens["refresh_token"])
    assert stolen.status_code == 400
    assert stolen.json()["error"] == "invalid_grant"

    row = _row_for(store, tokens["refresh_token"])
    assert row.revoked_at is None, "a wrong-client attempt must not rotate or revoke the token"
    assert refresh_with(client, owner, tokens["refresh_token"]).status_code == 200


def test_another_client_cannot_revoke_it(client, store):
    owner, tokens = full_flow(client, store)
    thief = registered_client(client)
    assert _revoke(client, thief, tokens["refresh_token"]).status_code == 200  # RFC 7009: opaque
    assert _row_for(store, tokens["refresh_token"]).revoked_at is None
    assert refresh_with(client, owner, tokens["refresh_token"]).status_code == 200


def test_the_owning_client_can_revoke_it(client, store):
    owner, tokens = full_flow(client, store)
    assert _revoke(client, owner, tokens["refresh_token"]).status_code == 200
    assert _row_for(store, tokens["refresh_token"]).revoked_at is not None
    assert refresh_with(client, owner, tokens["refresh_token"]).status_code == 400


def test_rotation_keeps_the_binding(client, store):
    owner, tokens = full_flow(client, store)
    rotated = refresh_with(client, owner, tokens["refresh_token"]).json()
    assert rotated["refresh_token"] != tokens["refresh_token"]
    assert _row_for(store, rotated["refresh_token"]).oauth_client_id == owner["client_id"]
    other = registered_client(client)
    assert refresh_with(client, other, rotated["refresh_token"]).status_code == 400


async def test_reuse_detection_still_revokes_the_whole_family(client, store):
    """Replaying an already-rotated token (by its own client) still trips the
    family revocation in `core.auth.service.refresh`."""
    class _Tx:
        """Reuse handling owns the request's transaction (rollback, revoke, commit); the in-memory repository
        needs no real session, only these two calls."""

        async def rollback(self) -> None:
            return None

        async def commit(self) -> None:
            return None

    owner, tokens = full_flow(client, store)
    request = RefreshRequest(refresh_token=tokens["refresh_token"])
    rotated = await auth_service.refresh(None, request, oauth_client_id=owner["client_id"])
    with pytest.raises(PermissionDeniedError) as excinfo:
        await auth_service.refresh(_Tx(), request, oauth_client_id=owner["client_id"])
    assert excinfo.value.error_code == "auth.refresh_token_reused"
    assert _row_for(store, rotated.refresh_token).revoked_at is not None


def test_a_rest_session_refresh_token_cannot_be_used_through_an_oauth_client(client, store):
    """An unbound (REST) refresh token is not an OAuth credential."""
    row = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=store.user_id,
        organization_id=store.organization_id,
        family_id=uuid.uuid4(),
        token_hash=hashlib.sha256(b"rest-token").hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(days=1),
        revoked_at=None,
        oauth_client_id=None,
    )
    store.refresh_rows[row.token_hash] = row
    info = registered_client(client)
    assert refresh_with(client, info, "rest-token").status_code == 400
    assert row.revoked_at is None


async def test_rest_refresh_cannot_consume_an_oauth_bound_token(client, store):
    """The reverse direction: the plain REST refresh/logout path (no client id)
    refuses a token that belongs to an OAuth client, without rotating it."""
    owner, tokens = full_flow(client, store)
    with pytest.raises(PermissionDeniedError):
        await auth_service.refresh(None, RefreshRequest(refresh_token=tokens["refresh_token"]))
    await auth_service.logout(None, RefreshRequest(refresh_token=tokens["refresh_token"]))
    assert _row_for(store, tokens["refresh_token"]).revoked_at is None
    assert refresh_with(client, owner, tokens["refresh_token"]).status_code == 200


async def test_rest_refresh_is_unchanged_for_unbound_tokens(store):
    tokens = await auth_service._issue_session(
        None, user_id=store.user_id, organization_id=store.organization_id, family_id=uuid.uuid4()
    )
    row = _row_for(store, tokens.refresh_token)
    assert row.oauth_client_id is None
    rotated = await auth_service.refresh(None, RefreshRequest(refresh_token=tokens.refresh_token))
    assert _row_for(store, rotated.refresh_token).oauth_client_id is None
    assert row.revoked_at is not None
