"""Password policy and hashing.

Owned by: core/auth. One place for every rule about *setting* a password
(signup, invitation acceptance, reset, change) and for hashing / verifying it.

Policy (new passwords only): 12-128 characters, never silently truncated, not
the account's own email. Existing passwords are never re-validated -- someone
who signed up with 8 characters can still sign in; the rule applies the next
time they set one.

Hashing: **Argon2id** for every new hash. Existing **bcrypt** hashes keep
verifying (the hash string self-identifies: `$2b$...` vs `$argon2id$...`), and
`needs_rehash` tells the login path to upgrade them -- after a *successful*
verification only, so a wrong password can never change a stored hash and no
account is locked out by the migration. bcrypt refuses inputs over 72 bytes
(the `bcrypt` package raises instead of truncating); such an input can never
match a bcrypt hash made by this code, so verification treats it as a mismatch.

The functions are synchronous (CPU/memory bound, ~0.3 s at the default cost);
async callers run them with `asyncio.to_thread` so they never stall the event
loop.
"""

from __future__ import annotations

import re
import secrets
from functools import lru_cache

import bcrypt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.exceptions import ValidationError
from app.shared.config.settings import get_settings

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128
#: Upper bound accepted by *login* (not a policy): enough for any password this
#: system could have stored, small enough that nobody can make us hash megabytes.
MAX_LOGIN_PASSWORD_LENGTH = 1024


def validate_new_password(password: str, *, email: str | None = None) -> None:
    """Raise `ValidationError` unless `password` may be set on an account.

    Length is counted in characters. Nothing is trimmed or truncated: a password
    that is too long is rejected, not shortened.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
            error_code="auth.password_too_short",
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValidationError(
            f"Password must be at most {MAX_PASSWORD_LENGTH} characters.",
            error_code="auth.password_too_long",
        )
    if not password.strip():
        raise ValidationError(
            "Password cannot be only spaces.", error_code="auth.password_blank"
        )
    if email is not None and password.strip().lower() == email.strip().lower():
        raise ValidationError(
            "Password cannot be the same as your email address.",
            error_code="auth.password_same_as_email",
        )


@lru_cache(maxsize=4)
def _hasher(memory_kib: int, time_cost: int, parallelism: int) -> PasswordHasher:
    return PasswordHasher(
        time_cost=time_cost, memory_cost=memory_kib, parallelism=parallelism, type=Type.ID
    )


def _current_hasher() -> PasswordHasher:
    s = get_settings()
    return _hasher(s.argon2_memory_cost_kib, s.argon2_time_cost, s.argon2_parallelism)


def hash_password(password: str) -> str:
    """Argon2id hash of `password` (salted, parameters embedded in the string)."""
    return _current_hasher().hash(password)


_BCRYPT_FORMAT = re.compile(r"\$2[abxy]\$\d{2}\$[./A-Za-z0-9]{53}")


def is_bcrypt_hash(password_hash: str) -> bool:
    """A well-formed bcrypt hash (60 characters). A truncated or mangled `$2b$...` string is
    not one, so it is never offered for upgrade and never verifies."""
    return _BCRYPT_FORMAT.fullmatch(password_hash) is not None


def verify_password(password: str, password_hash: str) -> bool:
    """True only if `password` matches `password_hash` (Argon2id or legacy bcrypt).
    Never raises for a malformed hash or an over-long bcrypt input -- those are
    simply "no match"."""
    try:
        if is_bcrypt_hash(password_hash):
            return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
        return _current_hasher().verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError, ValueError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True for every bcrypt hash and for an Argon2 hash made with weaker or
    different parameters than the current settings."""
    if is_bcrypt_hash(password_hash):
        return True
    try:
        return _current_hasher().check_needs_rehash(password_hash)
    except (InvalidHashError, ValueError):
        return False


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


def verify_against_dummy(password: str) -> None:
    """Spend the same work as a real verification when there is no account (or no
    password) to check, so response time does not reveal which emails exist."""
    verify_password(password, _dummy_hash())
