"""Key Management Service abstraction (PROJECT_PLAN.md section 12.5).

`KeyManagementService` is the seam between "envelope encryption logic"
(`envelope.py`, which never changes) and "who actually holds the
key-encryption-key" (this file, which is exactly what changes when this
codebase moves from local development to a real production deployment).

`LocalKeyManagementService` is a deliberate, explicitly-flagged
pre-production stand-in for a real cloud KMS (AWS KMS, GCP Cloud KMS, Azure
Key Vault). It holds its KEK as a plain settings value
(`connector_secret_master_key`) rather than in a separate, hardware-backed
trust boundary -- meaning, unlike section 12.5's stated property ("a
database compromise alone does not expose customer connector credentials --
the KMS is a separate trust boundary that must also be compromised"), a
compromise of this application's own runtime environment (which already has
the KEK in memory/settings) *would* be sufficient to decrypt every stored
credential. This is the one property a real KMS integration would fix:
`generate_data_key`/`decrypt_data_key` would become network calls to a KMS
that never releases the KEK itself, only ever performs the wrap/unwrap
operation server-side. Swapping in a real KMS client means writing one new
class satisfying this same `KeyManagementService` protocol -- no caller of
`envelope.encrypt_secret`/`decrypt_secret` changes.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.shared.config.settings import get_settings

_NONCE_SIZE_BYTES = 12  # AES-GCM's standard/recommended nonce size.
_DEK_SIZE_BYTES = 32  # AES-256.


class KeyManagementService(Protocol):
    """Wraps/unwraps a per-secret data-encryption-key (DEK) using a
    key-encryption-key (KEK) this interface deliberately never exposes to
    its callers -- `envelope.py` only ever sees plaintext/encrypted DEKs,
    never the KEK itself, so a real KMS-backed implementation can keep the
    KEK entirely server-side without changing this contract.
    """

    def generate_data_key(self) -> tuple[bytes, bytes]:
        """Generate a fresh random DEK, returning `(plaintext_dek,
        encrypted_dek)`. Called once per secret encrypted -- a fresh DEK per
        secret, not one DEK reused across every secret, so compromising one
        decrypted DEK never exposes any other secret.
        """
        ...

    def decrypt_data_key(self, encrypted_dek: bytes) -> bytes:
        """Unwrap a previously-encrypted DEK back to its plaintext bytes."""
        ...


class LocalKeyManagementService:
    """Pre-production stand-in for a real cloud KMS -- see module docstring
    for exactly what property this does and does not provide.
    """

    def __init__(self, kek: bytes) -> None:
        if len(kek) != _DEK_SIZE_BYTES:
            raise ValueError(
                f"KEK must be exactly {_DEK_SIZE_BYTES} bytes, got {len(kek)}."
            )
        self._kek = kek

    def generate_data_key(self) -> tuple[bytes, bytes]:
        dek = os.urandom(_DEK_SIZE_BYTES)
        nonce = os.urandom(_NONCE_SIZE_BYTES)
        ciphertext = AESGCM(self._kek).encrypt(nonce, dek, None)
        # Nonce is not secret -- it's packed alongside the ciphertext (a
        # standard AES-GCM convention) since `decrypt_data_key` needs it back
        # and there is nowhere else to carry it in this interface's shape.
        encrypted_dek = nonce + ciphertext
        return dek, encrypted_dek

    def decrypt_data_key(self, encrypted_dek: bytes) -> bytes:
        nonce, ciphertext = (
            encrypted_dek[:_NONCE_SIZE_BYTES],
            encrypted_dek[_NONCE_SIZE_BYTES:],
        )
        return AESGCM(self._kek).decrypt(nonce, ciphertext, None)


@lru_cache
def get_kms() -> KeyManagementService:
    """Cached accessor -- same `@lru_cache`-wrapped-singleton pattern
    `shared.config.settings.get_settings` already established, so every
    caller shares one `LocalKeyManagementService` instance (and, by
    extension, one already-decoded KEK) rather than re-parsing
    `connector_secret_master_key` on every call.
    """
    settings = get_settings()
    kek = bytes.fromhex(settings.connector_secret_master_key)
    return LocalKeyManagementService(kek)
