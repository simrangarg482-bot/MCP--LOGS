"""Regression coverage for `app.shared.config.settings.Settings`.

`cors_allowed_origins` previously crashed the whole app at process startup
when set via a real `CORS_ALLOWED_ORIGINS` environment variable (as opposed
to left at its default): pydantic-settings attempts to JSON-decode any
list-typed field's raw env value before this field's own `_split_cors_origins`
validator ever runs, so a real comma-separated value (exactly what that
validator's docstring says it accepts) raised `SettingsError` from a failed
`json.loads`, not a clean validation error. Caught by an actual browser E2E
run (`frontend/e2e/`) that started the real server with a non-default
`CORS_ALLOWED_ORIGINS` -- nothing in the existing unit test suite ever
constructs `Settings()` against a real environment variable override.
"""

from __future__ import annotations

from app.shared.config.settings import Settings


def test_cors_allowed_origins_parses_a_comma_separated_env_var(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:5180,http://127.0.0.1:5180")

    settings = Settings()

    assert settings.cors_allowed_origins == ["http://localhost:5180", "http://127.0.0.1:5180"]


def test_cors_allowed_origins_defaults_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)

    settings = Settings()

    assert settings.cors_allowed_origins == ["http://localhost:5173", "http://127.0.0.1:5173"]


def test_cors_allowed_origins_parses_a_single_origin_env_var(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://app.example.com")

    settings = Settings()

    assert settings.cors_allowed_origins == ["https://app.example.com"]
