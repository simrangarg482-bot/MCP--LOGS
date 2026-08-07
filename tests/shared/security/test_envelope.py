"""Tests for `app.shared.security.envelope` -- `encrypt_secret`/
`decrypt_secret`'s round trip, and tamper/version-mismatch handling.
"""
from __future__ import annotations
import json
import os
import pytest
from app.shared.security.envelope import decrypt_secret, encrypt_secret
from app.shared.security.kms import LocalKeyManagementService
def _kms() -> LocalKeyManagementService:
    return LocalKeyManagementService(os.urandom(32))
def test_encrypt_then_decrypt_round_trips() -> None:
    kms = _kms()
    plaintext = "xoxb-11725744885042-fake-slack-bot-token"

    encrypted = encrypt_secret(kms, plaintext)
    assert plaintext not in encrypted  # never stored in the clear

    assert decrypt_secret(kms, encrypted) == plaintext


def test_encrypt_produces_a_versioned_json_envelope() -> None:
    kms = _kms()
    encrypted = encrypt_secret(kms, "some-credential")
    envelope = json.loads(encrypted)

    assert envelope["v"] == 1
    assert set(envelope) == {"v", "encrypted_dek", "nonce", "ciphertext"}
def test_encrypting_the_same_secret_twice_produces_different_ciphertext() -> None:
    """A fresh DEK (and nonce) per call -- see `encrypt_secret`'s own
    docstring on why one compromised secret must not expose any other.
    """
    kms = _kms()
    first = encrypt_secret(kms, "same-value")
    second = encrypt_secret(kms, "same-value")

    assert first != second
    assert decrypt_secret(kms, first) == "same-value"
    assert decrypt_secret(kms, second) == "same-value"
def test_decrypt_rejects_unsupported_envelope_version() -> None:
    kms = _kms()
    encrypted = encrypt_secret(kms, "some-credential")
    envelope = json.loads(encrypted)
    envelope["v"] = 99
    tampered = json.dumps(envelope)

    with pytest.raises(ValueError):
        decrypt_secret(kms, tampered)


def test_decrypt_rejects_tampered_ciphertext() -> None:
    kms = _kms()
    encrypted = encrypt_secret(kms, "some-credential")
    envelope = json.loads(encrypted)
    # Flip the ciphertext to something else, still valid base64.
    envelope["ciphertext"] = envelope["ciphertext"][:-4] + "AAAA"
    tampered = json.dumps(envelope)

    with pytest.raises(Exception):
        decrypt_secret(kms, tampered)


def test_decrypt_with_wrong_kms_fails() -> None:
    kms_a = _kms()
    kms_b = _kms()
    encrypted = encrypt_secret(kms_a, "some-credential")

    with pytest.raises(Exception):
        decrypt_secret(kms_b, encrypted)
