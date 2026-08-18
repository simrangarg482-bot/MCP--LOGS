"""Tests for `app.shared.security.kms` -- `LocalKeyManagementService`'s
wrap/unwrap round trip, and `get_kms`'s settings-driven construction.

Phase 3: both `generate_data_key`/`decrypt_data_key` became `async def`
(real I/O for a real KMS -- see kms.py's module docstring) and
`generate_data_key` now returns a 3-tuple `(dek, encrypted_dek,
key_version)`, not 2 -- `LocalKeyManagementService` ignores `key_version`
entirely (it has exactly one KEK, never rotated), but every caller/test
still has to thread it through to satisfy the shared protocol.
"""

from __future__ import annotations

import os

import pytest

from app.shared.security.kms import LocalKeyManagementService, get_kms


def _kms(kek: bytes | None = None) -> LocalKeyManagementService:
    return LocalKeyManagementService(kek or os.urandom(32))


def test_rejects_kek_of_wrong_length() -> None:
    with pytest.raises(ValueError):
        LocalKeyManagementService(b"too-short")


@pytest.mark.asyncio
async def test_generate_data_key_round_trips() -> None:
    kms = _kms()
    dek, encrypted_dek, key_version = await kms.generate_data_key()

    assert len(dek) == 32
    assert encrypted_dek != dek  # actually wrapped, not passed through
    assert await kms.decrypt_data_key(encrypted_dek, key_version) == dek


@pytest.mark.asyncio
async def test_generate_data_key_produces_fresh_dek_each_call() -> None:
    kms = _kms()
    dek_one, _, _ = await kms.generate_data_key()
    dek_two, _, _ = await kms.generate_data_key()

    assert dek_one != dek_two


@pytest.mark.asyncio
async def test_decrypt_data_key_fails_with_wrong_kek() -> None:
    kms_a = _kms()
    kms_b = _kms()
    _, encrypted_dek, key_version = await kms_a.generate_data_key()

    with pytest.raises(Exception):
        await kms_b.decrypt_data_key(encrypted_dek, key_version)


@pytest.mark.asyncio
async def test_get_kms_returns_a_working_local_kms() -> None:
    # Relies on CONNECTOR_SECRET_MASTER_KEY being set in the test environment
    # (`.env`, same as every other setting `get_settings()` requires --
    # `jwt_secret_key`, `database_url`, etc.).
    kms = get_kms()
    dek, encrypted_dek, key_version = await kms.generate_data_key()
    assert await kms.decrypt_data_key(encrypted_dek, key_version) == dek
