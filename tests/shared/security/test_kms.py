"""Tests for `app.shared.security.kms` -- `LocalKeyManagementService`'s
wrap/unwrap round trip, and `get_kms`'s settings-driven construction.
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


def test_generate_data_key_round_trips() -> None:
    kms = _kms()
    dek, encrypted_dek = kms.generate_data_key()

    assert len(dek) == 32
    assert encrypted_dek != dek  # actually wrapped, not passed through
    assert kms.decrypt_data_key(encrypted_dek) == dek


def test_generate_data_key_produces_fresh_dek_each_call() -> None:
    kms = _kms()
    dek_one, _ = kms.generate_data_key()
    dek_two, _ = kms.generate_data_key()

    assert dek_one != dek_two


def test_decrypt_data_key_fails_with_wrong_kek() -> None:
    kms_a = _kms()
    kms_b = _kms()
    _, encrypted_dek = kms_a.generate_data_key()

    with pytest.raises(Exception):
        kms_b.decrypt_data_key(encrypted_dek)


def test_get_kms_returns_a_working_local_kms() -> None:
    # Relies on CONNECTOR_SECRET_MASTER_KEY being set in the test environment
    # (`.env`, same as every other setting `get_settings()` requires --
    # `jwt_secret_key`, `database_url`, etc.).
    kms = get_kms()
    dek, encrypted_dek = kms.generate_data_key()
    assert kms.decrypt_data_key(encrypted_dek) == dek
