"""Tests for `app.shared.security.envelope` -- `encrypt_secret`/
`decrypt_secret`'s round trip, and tamper/version-mismatch handling.

Phase 3: both functions became `async def` (the real Azure KMS provider's
wrap/unwrap calls are real network I/O -- see kms.py's module docstring),
and the envelope gained a `key_version` field (bumping `v` to 2) recording
which KMS key/version wrapped each DEK, needed for Key Vault's per-version
rotation model.
"""
from __future__ import annotations
import json
import os
import pytest
from app.shared.security.envelope import decrypt_secret, encrypt_secret
from app.shared.security.kms import LocalKeyManagementService
def _kms() -> LocalKeyManagementService:
    return LocalKeyManagementService(os.urandom(32))


@pytest.mark.asyncio
async def test_encrypt_then_decrypt_round_trips() -> None:
    kms = _kms()
    plaintext = "xoxb-11725744885042-fake-slack-bot-token"

    encrypted = await encrypt_secret(kms, plaintext)
    assert plaintext not in encrypted  # never stored in the clear

    assert await decrypt_secret(kms, encrypted) == plaintext


@pytest.mark.asyncio
async def test_encrypt_produces_a_versioned_json_envelope() -> None:
    kms = _kms()
    encrypted = await encrypt_secret(kms, "some-credential")
    envelope = json.loads(encrypted)

    assert envelope["v"] == 2
    assert set(envelope) == {"v", "key_version", "encrypted_dek", "nonce", "ciphertext"}


@pytest.mark.asyncio
async def test_encrypting_the_same_secret_twice_produces_different_ciphertext() -> None:
    """A fresh DEK (and nonce) per call -- see `encrypt_secret`'s own
    docstring on why one compromised secret must not expose any other.
    """
    kms = _kms()
    first = await encrypt_secret(kms, "same-value")
    second = await encrypt_secret(kms, "same-value")

    assert first != second
    assert await decrypt_secret(kms, first) == "same-value"
    assert await decrypt_secret(kms, second) == "same-value"


@pytest.mark.asyncio
async def test_decrypt_accepts_a_legacy_v1_envelope_with_no_key_version() -> None:
    """Backward compatibility: every secret encrypted before this Phase 3
    change was stored as a `v=1` envelope with no `key_version` field at
    all. `decrypt_secret` must still read it -- see envelope.py's module
    docstring for why passing `LocalKeyManagementService` a missing
    `key_version` (`None`) is exactly as correct as any placeholder a v2
    envelope would have carried.
    """
    kms = _kms()
    encrypted = await encrypt_secret(kms, "some-credential")
    envelope = json.loads(encrypted)
    legacy = {k: v for k, v in envelope.items() if k != "key_version"}
    legacy["v"] = 1

    assert await decrypt_secret(kms, json.dumps(legacy)) == "some-credential"


@pytest.mark.asyncio
async def test_decrypt_rejects_unsupported_envelope_version() -> None:
    kms = _kms()
    encrypted = await encrypt_secret(kms, "some-credential")
    envelope = json.loads(encrypted)
    envelope["v"] = 99
    tampered = json.dumps(envelope)

    with pytest.raises(ValueError):
        await decrypt_secret(kms, tampered)


@pytest.mark.asyncio
async def test_decrypt_rejects_tampered_ciphertext() -> None:
    kms = _kms()
    encrypted = await encrypt_secret(kms, "some-credential")
    envelope = json.loads(encrypted)
    # Flip the ciphertext to something else, still valid base64.
    envelope["ciphertext"] = envelope["ciphertext"][:-4] + "AAAA"
    tampered = json.dumps(envelope)

    with pytest.raises(Exception):
        await decrypt_secret(kms, tampered)


@pytest.mark.asyncio
async def test_decrypt_with_wrong_kms_fails() -> None:
    kms_a = _kms()
    kms_b = _kms()
    encrypted = await encrypt_secret(kms_a, "some-credential")

    with pytest.raises(Exception):
        await decrypt_secret(kms_b, encrypted)
