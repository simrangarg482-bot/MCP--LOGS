"""Password policy and hashing (`core.auth.passwords`): Argon2id for new hashes, legacy
bcrypt still verifies, never silent truncation, nothing raises on garbage."""

from __future__ import annotations

import bcrypt
import pytest
from argon2 import PasswordHasher, Type

from app.core.auth import passwords
from app.core.exceptions import ValidationError

GOOD = "correct horse battery staple"


@pytest.mark.parametrize(
    ("password", "code"),
    [
        ("short", "auth.password_too_short"),
        ("a" * 11, "auth.password_too_short"),
        ("a" * 129, "auth.password_too_long"),
        ("            ", "auth.password_blank"),
        ("Person@Corp.test", None),  # exactly the email: handled below
    ],
)
def test_policy_rejects_with_stable_error_codes(password, code) -> None:
    if code is None:
        with pytest.raises(ValidationError) as info:
            passwords.validate_new_password("Person@Corp.test ", email="person@corp.test")
        assert info.value.error_code == "auth.password_same_as_email"
        return
    with pytest.raises(ValidationError) as info:
        passwords.validate_new_password(password)
    assert info.value.error_code == code


@pytest.mark.parametrize("password", ["a" * 12, "a" * 128, GOOD, "pässwörd-with-ünïcode-1", "a b c d e f g h i"])
def test_policy_accepts_the_boundaries_and_unicode(password) -> None:
    passwords.validate_new_password(password)


def test_length_counts_characters_not_bytes() -> None:
    # 12 two-byte characters = 24 bytes: valid. 129 characters is too long however many bytes.
    passwords.validate_new_password("é" * 12)
    with pytest.raises(ValidationError):
        passwords.validate_new_password("é" * 129)


def test_new_hashes_are_argon2id_and_salted() -> None:
    first, second = passwords.hash_password(GOOD), passwords.hash_password(GOOD)
    assert first.startswith("$argon2id$") and first != second
    assert passwords.verify_password(GOOD, first) and passwords.verify_password(GOOD, second)
    assert not passwords.verify_password(GOOD + "x", first)
    assert not passwords.verify_password("", first)
    assert not passwords.needs_rehash(first)


def test_legacy_bcrypt_hashes_still_verify_and_are_marked_for_upgrade() -> None:
    legacy = bcrypt.hashpw(GOOD.encode(), bcrypt.gensalt(rounds=4)).decode()
    assert legacy.startswith("$2")
    assert passwords.verify_password(GOOD, legacy)
    assert not passwords.verify_password("wrong password!", legacy)
    assert passwords.needs_rehash(legacy)


def test_an_over_long_input_never_matches_a_bcrypt_hash_by_truncation() -> None:
    """bcrypt only sees 72 bytes. The old behaviour for a longer password was to silently ignore
    the rest; now the extra bytes must make it NOT match (bcrypt 5 refuses; we report no match)."""
    base = "x" * 72
    legacy = bcrypt.hashpw(base.encode(), bcrypt.gensalt(rounds=4)).decode()
    assert passwords.verify_password(base, legacy)
    assert not passwords.verify_password(base + "tail", legacy)  # would be True under truncation


def test_a_weaker_argon2_hash_needs_rehash_and_still_verifies() -> None:
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, type=Type.ID).hash(GOOD)
    assert passwords.verify_password(GOOD, weak)
    assert passwords.needs_rehash(weak)


@pytest.mark.parametrize("garbage", ["", "not-a-hash", "$argon2id$broken", "$2b$12$tooshort", "plaintext-password"])
def test_malformed_hashes_never_raise_and_never_match(garbage) -> None:
    assert passwords.verify_password(GOOD, garbage) is False
    assert passwords.needs_rehash(garbage) is False  # unknown format: never "upgrade" it


def test_dummy_verification_does_not_raise() -> None:
    passwords.verify_against_dummy("whatever")
