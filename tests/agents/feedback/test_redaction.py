"""`agents.feedback.redaction` -- secrets must never survive into a stored
comment or an exported evaluation candidate, and the replacement must carry
the secret's KIND only, never any part of its value."""

from __future__ import annotations

import pytest

from app.agents.feedback.redaction import redact_secrets

# Built from fragments so this file itself never contains a string a secret
# scanner would flag. All values are well-known documentation examples or
# obviously fake.
_AWS = "AKIA" + "IOSFODNN7EXAMPLE"
_GH = "ghp_" + "a" * 36
_SLACK = "xoxb-" + "1234567890-abcdefghij"
_JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + "." + "dBjftJeZ4CVPmB92K27uhbUJU1p1r"


@pytest.mark.parametrize(
    ("secret", "kind"),
    [
        (_AWS, "aws_access_key"),
        (_GH, "github_token"),
        (_SLACK, "slack_token"),
        (_JWT, "jwt"),
        ("sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2", "openai_style_key"),
        ("Bearer " + "abcdef1234567890ABCDEF", "bearer_token"),
    ],
)
def test_known_secret_formats_are_replaced_by_their_kind(secret: str, kind: str) -> None:
    redacted, count = redact_secrets(f"it failed when I used {secret} in prod")

    assert secret not in redacted
    assert f"[REDACTED:{kind}]" in redacted
    assert count >= 1
    assert redacted.startswith("it failed when I used ")


def test_private_key_block_is_removed_entirely() -> None:
    block = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nabc123\n-----END RSA PRIVATE KEY-----"
    redacted, count = redact_secrets(f"here: {block} thanks")

    assert "MIIEowIBAAKCAQEA" not in redacted
    assert "abc123" not in redacted
    assert count == 1


def test_connection_string_keeps_host_but_drops_credentials() -> None:
    redacted, count = redact_secrets("db is postgresql://admin:hunter2pass@db.internal:5432/app")

    assert "hunter2pass" not in redacted
    assert "admin" not in redacted
    # Exactly: the scheme and host survive, only the credential is replaced.
    assert redacted == "db is postgresql://[REDACTED:url_credentials]@db.internal:5432/app"
    assert count == 1


@pytest.mark.parametrize("text", ["password=correcthorse", "api_key: 'abcd1234efgh'", 'token = "xyz98765"'])
def test_credential_assignments_are_redacted(text: str) -> None:
    redacted, count = redact_secrets(text)

    assert "[REDACTED:credential_assignment]" in redacted
    assert count == 1
    for leaked in ("correcthorse", "abcd1234efgh", "xyz98765"):
        assert leaked not in redacted
    # The key name and separator are kept (so a reader can see WHAT was
    # redacted) and no stray control characters are introduced.
    key = text.split("=")[0].split(":")[0].strip()
    assert redacted.startswith(key)
    assert all(character.isprintable() for character in redacted)


def test_surrounding_text_survives_a_credential_assignment_exactly() -> None:
    redacted, _ = redact_secrets("use password=correcthorse for staging")

    assert redacted == "use password=[REDACTED:credential_assignment] for staging"


def test_ordinary_prose_is_untouched() -> None:
    text = "The answer cited the old runbook; the retry token bucket was renamed in v2."
    redacted, count = redact_secrets(text)

    assert redacted == text
    assert count == 0


def test_multiple_secrets_are_all_counted() -> None:
    redacted, count = redact_secrets(f"{_AWS} and {_GH}")

    assert count == 2
    assert _AWS not in redacted and _GH not in redacted


def test_redaction_is_idempotent() -> None:
    once, _ = redact_secrets(f"password=correcthorse and {_AWS}")
    twice, extra = redact_secrets(once)

    assert twice == once
    assert extra == 0
