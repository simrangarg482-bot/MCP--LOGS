"""Envelope encryption for a single secret string (PROJECT_PLAN.md section
12.5) -- pure functions over a `KeyManagementService` (`kms.py`), no I/O of
their own beyond what `kms` performs (both functions are `async def` purely
because `kms`'s real, Azure-backed operations are; the local provider does
no I/O at all).

Envelope shape (serialized as JSON, stored verbatim as `connector_configs.
credential_ref`/`sso_configurations.client_secret_ref`/
`mcp_oauth_clients.client_secret_encrypted` -- see this package's
`__init__.py` docstring on why there is no separate secrets-record table
yet):
    {"v": 2, "key_version": "<opaque>", "encrypted_dek": "<base64>",
     "nonce": "<base64>", "ciphertext": "<base64>"}

`"v"` is a schema version tag from the start -- cheap to add now, expensive
to retrofit onto already-stored ciphertext later if the envelope shape ever
needs to change.

**Phase 3 addition**: `"key_version"` (bumping the envelope to `v=2`) records
whichever KMS key/version `kms.generate_data_key()` used to wrap this
secret's DEK -- required for Azure Key Vault's rotation model, where
unwrapping is inherently per-key-version (see `kms.py`'s module docstring).
`decrypt_secret` still accepts a `v=1` envelope with no `key_version` field
at all -- every secret already stored under the pre-Phase-3 local-only KMS
was necessarily wrapped by `LocalKeyManagementService`'s one fixed KEK, which
ignores `key_version` entirely (see that class's `decrypt_data_key`), so
passing it `None` for a legacy envelope is exactly as correct as whatever
placeholder a v2 envelope would have carried. This is what makes existing
encrypted values remain readable across this change with no data migration:
old rows are read as-is, forever, as long as the deployment's KMS provider
can still unwrap them (true for `local`; a deployment that has since fully
cut over to `azure` and decommissioned its local KEK would need to have
already re-encrypted any legacy `v=1` values first -- a one-time migration
step, not something this function silently performs).
"""

from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.shared.security.kms import KeyManagementService

_ENVELOPE_VERSION = 2
_NONCE_SIZE_BYTES = 12


async def encrypt_secret(kms: KeyManagementService, plaintext: str) -> str:
    """Encrypt `plaintext` (a connector credential, SSO client secret, or
    MCP OAuth client secret) into a serialized envelope string.

    A fresh DEK is generated per call (`kms.generate_data_key`) -- see that
    method's own docstring on why one compromised secret must not expose any
    other. The DEK itself is held only for the duration of this function
    call, never returned or logged.
    """
    dek, encrypted_dek, key_version = await kms.generate_data_key()
    nonce = os.urandom(_NONCE_SIZE_BYTES)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), None)

    envelope = {
        "v": _ENVELOPE_VERSION,
        "key_version": key_version,
        "encrypted_dek": base64.b64encode(encrypted_dek).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    return json.dumps(envelope)


async def decrypt_secret(kms: KeyManagementService, encrypted: str) -> str:
    """Reverse `encrypt_secret`. Raises `ValueError`/`json.JSONDecodeError`
    if `encrypted` is not a well-formed envelope of a version this function
    understands, and whatever `cryptography` raises
    (`InvalidTag`) if the ciphertext or encrypted DEK has been tampered with
    or doesn't match this KMS's KEK -- none of these are caught and
    silenced here, since a caller getting back garbage instead of a real
    credential is far more dangerous than a loud failure.
    """
    envelope = json.loads(encrypted)
    if envelope.get("v") not in (1, _ENVELOPE_VERSION):
        raise ValueError(f"Unsupported credential envelope version: {envelope.get('v')!r}")

    # `key_version` is absent on every pre-Phase-3 (`v=1`) envelope -- see
    # module docstring for why `None` is the correct, safe value to pass a
    # `LocalKeyManagementService` for one of those.
    key_version = envelope.get("key_version")
    encrypted_dek = base64.b64decode(envelope["encrypted_dek"])
    nonce = base64.b64decode(envelope["nonce"])
    ciphertext = base64.b64decode(envelope["ciphertext"])

    dek = await kms.decrypt_data_key(encrypted_dek, key_version)
    plaintext = AESGCM(dek).decrypt(nonce, ciphertext, None)
    return plaintext.decode("utf-8")
