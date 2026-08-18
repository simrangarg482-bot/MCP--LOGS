"""Opaque single-use token hashing (Phase 7.5/7.6).

Owned by: shared/ -- genuinely cross-module: `core.tenancy.service.
create_invitation` (a new random token, hashed at rest here) and
`core.auth.service`'s own refresh-token hashing (`_hash_token`, unchanged,
kept private to that module rather than migrated here to limit the blast
radius of this change) both need identical logic, and neither module owns
the other.

SHA-256, not a slow password hash (bcrypt/argon2/scrypt): the input here is
always a high-entropy, server-generated random value (`secrets.
token_urlsafe`), never a human-chosen secret -- there is nothing for an
attacker to dictionary/brute-force from a leaked hash the way there is for a
password, so a fast, deterministic hash used purely for lookup-by-hash is
the correct and sufficient choice, matching `core.auth.service._hash_token`'s
own identical reasoning for refresh tokens.
"""

from __future__ import annotations

import hashlib
import secrets


def generate_opaque_token(*, num_bytes: int = 32) -> str:
    """A new, high-entropy, URL-safe random token -- the raw value shown to
    a caller exactly once (e.g. embedded in an invitation link), never
    stored anywhere in this form.
    """
    return secrets.token_urlsafe(num_bytes)


def hash_opaque_token(raw_token: str) -> str:
    """Hash `raw_token` for storage/lookup -- never store the raw value
    itself (section 12.1's "never stored in plaintext" discipline, applied
    here to invitation tokens the same way `core.auth.service._hash_token`
    already applies it to refresh tokens).
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
