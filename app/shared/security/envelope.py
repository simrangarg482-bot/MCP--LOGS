"""Envelope encryption for a single secret string (PROJECT_PLAN.md section
12.5) -- pure functions over a `KeyManagementService` (`kms.py`), no I/O of
their own beyond what `kms` performs.

Envelope shape (serialized as JSON, stored verbatim as `connector_configs.
credential_ref` -- see this package's `__init__.py` docstring on why there
is no separate secrets-record table yet):
    {"v": 1, "encrypted_dek": "<base64>", "nonce": "<base64>", "ciphertext": "<base64>"}
`"v"` is a schema version tag from the start -- cheap to add now, expensive
to retrofit onto already-stored ciphertext later if the envelope shape ever
needs to change (e.g. a future KMS migration wanting a different AEAD or an
additional authenticated-context field).
"""

from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.shared.security.kms import KeyManagementService

_ENVELOPE_VERSION = 1
_NONCE_SIZE_BYTES = 12


def encrypt_secret(kms: KeyManagementService, plaintext: str) -> str:
    """Encrypt `plaintext` (a connector credential) into a serialized
    envelope string.

    A fresh DEK is generated per call (`kms.generate_data_key`) -- see that
    method's own docstring on why one compromised secret must not expose any
    other. The DEK itself is held only for the duration of this function
    call, never returned or logged.
    """
    dek, encrypted_dek = kms.generate_data_key()
    nonce = os.urandom(_NONCE_SIZE_BYTES)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), None)

    envelope = {
        "v": _ENVELOPE_VERSION,
        "encrypted_dek": base64.b64encode(encrypted_dek).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    return json.dumps(envelope)


def decrypt_secret(kms: KeyManagementService, encrypted: str) -> str:
    """Reverse `encrypt_secret`. Raises `ValueError`/`json.JSONDecodeError`
    if `encrypted` is not a well-formed envelope of a version this function
    understands, and whatever `cryptography` raises
    (`InvalidTag`) if the ciphertext or encrypted DEK has been tampered with
    or doesn't match this KMS's KEK -- none of these are caught and
    silenced here, since a caller getting back garbage instead of a real
    credential is far more dangerous than a loud failure.
    """
    envelope = json.loads(encrypted)
    if envelope.get("v") != _ENVELOPE_VERSION:
        raise ValueError(f"Unsupported credential envelope version: {envelope.get('v')!r}")

    encrypted_dek = base64.b64decode(envelope["encrypted_dek"])
    nonce = base64.b64decode(envelope["nonce"])
    ciphertext = base64.b64decode(envelope["ciphertext"])

    dek = kms.decrypt_data_key(encrypted_dek)
    plaintext = AESGCM(dek).decrypt(nonce, ciphertext, None)
    return plaintext.decode("utf-8")
