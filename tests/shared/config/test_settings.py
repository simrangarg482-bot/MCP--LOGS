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

import pytest
from pydantic import ValidationError

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


def test_cors_allowed_origins_rejects_a_bare_wildcard(monkeypatch) -> None:
    """Regression guard for the Phase 3 production-hardening pass: combined
    with `app.api.main.create_app`'s hardcoded `allow_credentials=True`, a
    wildcard origin would let Starlette reflect any requesting origin back
    as allowed -- see `Settings._reject_wildcard_origin`'s docstring.
    """
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "*")

    with pytest.raises(ValidationError, match="must not contain"):
        Settings()


def test_cors_allowed_origins_rejects_a_wildcard_mixed_with_real_origins(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://app.example.com,*")

    with pytest.raises(ValidationError, match="must not contain"):
        Settings()


# --- KMS provider selection (Phase 3: Azure Key Vault) -------------------------


def test_kms_provider_defaults_to_local(monkeypatch) -> None:
    monkeypatch.delenv("KMS_PROVIDER", raising=False)

    assert Settings().kms_provider == "local"


def test_kms_provider_azure_requires_vault_url_and_key_name(monkeypatch) -> None:
    monkeypatch.setenv("KMS_PROVIDER", "azure")
    monkeypatch.delenv("AZURE_KEY_VAULT_URL", raising=False)
    monkeypatch.delenv("AZURE_KEY_VAULT_KEY_NAME", raising=False)

    with pytest.raises(ValidationError, match="azure_key_vault_url"):
        Settings()


def test_kms_provider_azure_requires_key_name_even_if_url_is_set(monkeypatch) -> None:
    monkeypatch.setenv("KMS_PROVIDER", "azure")
    monkeypatch.setenv("AZURE_KEY_VAULT_URL", "https://ekip-prod.vault.azure.net")
    monkeypatch.delenv("AZURE_KEY_VAULT_KEY_NAME", raising=False)

    with pytest.raises(ValidationError, match="azure_key_vault_key_name"):
        Settings()


def test_kms_provider_azure_succeeds_with_both_vault_settings(monkeypatch) -> None:
    monkeypatch.setenv("KMS_PROVIDER", "azure")
    monkeypatch.setenv("AZURE_KEY_VAULT_URL", "https://ekip-prod.vault.azure.net")
    monkeypatch.setenv("AZURE_KEY_VAULT_KEY_NAME", "connector-secrets-kek")

    settings = Settings()

    assert settings.kms_provider == "azure"
    assert settings.azure_key_vault_url == "https://ekip-prod.vault.azure.net"
    assert settings.azure_key_vault_key_name == "connector-secrets-kek"


def test_kms_provider_local_requires_connector_secret_master_key(monkeypatch) -> None:
    monkeypatch.setenv("KMS_PROVIDER", "local")
    monkeypatch.setenv("CONNECTOR_SECRET_MASTER_KEY", "")

    with pytest.raises(ValidationError, match="connector_secret_master_key"):
        Settings()


def test_environment_production_rejects_kms_provider_local(monkeypatch) -> None:
    """PROJECT_PLAN.md's explicit requirement: production must never
    silently run with the development/test-only local KMS stand-in --
    checked at settings-construction (process-startup) time.
    """
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("KMS_PROVIDER", "local")

    with pytest.raises(ValidationError, match="not permitted when environment=production"):
        Settings()


def test_missing_required_settings_fails_clearly_with_no_env_file(monkeypatch) -> None:
    """Section 20's explicit production-safety requirement: with no `.env`
    file to fall back to (the real shape of a container/CI environment,
    unlike this repo's own local dev setup) and required variables unset,
    construction must fail with a clear, actionable error naming every
    missing field -- never silently default to a placeholder or partially
    construct.
    """
    for key in ("DATABASE_URL", "REDIS_URL", "JWT_SECRET_KEY", "OPENAI_API_KEY", "CONNECTOR_SECRET_MASTER_KEY"):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    missing_fields = {error["loc"][0] for error in exc_info.value.errors()}
    assert {"database_url", "redis_url", "jwt_secret_key", "openai_api_key"} <= missing_fields


def test_environment_production_accepts_kms_provider_azure(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("KMS_PROVIDER", "azure")
    monkeypatch.setenv("AZURE_KEY_VAULT_URL", "https://ekip-prod.vault.azure.net")
    monkeypatch.setenv("AZURE_KEY_VAULT_KEY_NAME", "connector-secrets-kek")

    settings = Settings()

    assert settings.environment == "production"
    assert settings.kms_provider == "azure"
