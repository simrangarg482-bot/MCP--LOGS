"""Tests for `app.shared.config.logging._redact_sensitive_fields` (Phase 5.1).

A safety-net processor, not the primary control -- these tests exist to pin
down exactly which key shapes it catches and which it deliberately leaves
alone, since a redaction processor that's too aggressive silently destroys
useful log data, and one that's too narrow provides false confidence.
"""

from __future__ import annotations

from app.shared.config.logging import _redact_sensitive_fields, _REDACTED


def _redact(event_dict: dict) -> dict:
    return _redact_sensitive_fields(None, "info", event_dict)


def test_redacts_exact_sensitive_key_names() -> None:
    result = _redact(
        {
            "password": "hunter2",
            "token": "abc123",
            "secret": "shh",
            "api_key": "sk-real",
            "authorization": "Bearer xyz",
            "credential": "cred-value",
            "client_secret": "cs-value",
            "private_key": "pk-value",
        }
    )
    for key in result:
        assert result[key] == _REDACTED, f"{key} was not redacted"


def test_redacts_substring_matches_not_just_exact_names() -> None:
    result = _redact(
        {
            "jwt_token": "abc123",
            "authorization_header": "Bearer xyz",
            "client_secret_ref": "vault-ref",
            "connector_api_key": "sk-real",
        }
    )
    for key in result:
        assert result[key] == _REDACTED, f"{key} was not redacted"


def test_redacts_nested_dict_values() -> None:
    result = _redact(
        {
            "request_summary": {
                "organization_id": "org-1",
                "password": "hunter2",
            }
        }
    )
    assert result["request_summary"]["organization_id"] == "org-1"
    assert result["request_summary"]["password"] == _REDACTED


def test_does_not_redact_safe_keys() -> None:
    result = _redact(
        {
            "event": "user_logged_in",
            "organization_id": "org-1",
            "user_id": "user-1",
            "request_id": "req-1",
            "status_code": 200,
            "duration_ms": 42,
        }
    )
    assert result == {
        "event": "user_logged_in",
        "organization_id": "org-1",
        "user_id": "user-1",
        "request_id": "req-1",
        "status_code": 200,
        "duration_ms": 42,
    }


def test_redaction_is_case_insensitive() -> None:
    result = _redact({"Password": "hunter2", "API_KEY": "sk-real"})
    assert result["Password"] == _REDACTED
    assert result["API_KEY"] == _REDACTED
