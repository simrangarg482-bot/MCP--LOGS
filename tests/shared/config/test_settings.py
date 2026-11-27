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


@pytest.fixture(autouse=True)
def _no_dev_sso_switch(monkeypatch):
    """A developer's own `.env` may turn on `SSO_ALLOW_INSECURE_ISSUERS` for local testing; these
    tests are about the defaults and the production rules, so pin it off."""
    monkeypatch.setenv("SSO_ALLOW_INSECURE_ISSUERS", "false")


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


def _set_valid_production_email_env(monkeypatch) -> None:
    """Production now also requires working email delivery (email verification
    is always enforced). The KMS tests below are about KMS, so they supply a
    valid email configuration to keep that the only thing under test."""
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("EMAIL_FROM_ADDRESS", "no-reply@example.com")
    monkeypatch.setenv("FRONTEND_PUBLIC_BASE_URL", "https://app.example.com")


def test_environment_production_rejects_kms_provider_local(monkeypatch) -> None:
    """PROJECT_PLAN.md's explicit requirement: production must never
    silently run with the development/test-only local KMS stand-in --
    checked at settings-construction (process-startup) time.
    """
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("KMS_PROVIDER", "local")
    _set_valid_production_email_env(monkeypatch)

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
    _set_valid_production_email_env(monkeypatch)

    settings = Settings()

    assert settings.environment == "production"
    assert settings.kms_provider == "azure"


def _production_with_azure_kms(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("KMS_PROVIDER", "azure")
    monkeypatch.setenv("AZURE_KEY_VAULT_URL", "https://ekip-prod.vault.azure.net")
    monkeypatch.setenv("AZURE_KEY_VAULT_KEY_NAME", "connector-secrets-kek")


def test_production_refuses_to_boot_without_email_delivery(monkeypatch) -> None:
    """Email verification is always enforced; production must never silently run
    with no way to send the verification mail (which would lock signups out) --
    it fails at process startup instead."""
    _production_with_azure_kms(monkeypatch)
    # Explicit values (not delenv): a developer's local .env must not be able to
    # make this pass or fail.
    monkeypatch.setenv("EMAIL_PROVIDER", "none")
    monkeypatch.setenv("EMAIL_FROM_ADDRESS", "")
    monkeypatch.setenv("SMTP_HOST", "")

    with pytest.raises(ValidationError, match="requires working email delivery"):
        Settings()


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"EMAIL_PROVIDER": "none"}, "EMAIL_PROVIDER must not be 'none'"),
        ({"EMAIL_FROM_ADDRESS": ""}, "EMAIL_FROM_ADDRESS is required"),
        ({"SMTP_HOST": ""}, "SMTP_HOST is required"),
        ({"SMTP_SECURITY": "none"}, "SMTP_SECURITY must be"),
        ({"FRONTEND_PUBLIC_BASE_URL": "http://app.example.com"}, "must be an https:// URL"),
    ],
)
def test_production_rejects_each_incomplete_email_setting(monkeypatch, override, expected) -> None:
    _production_with_azure_kms(monkeypatch)
    _set_valid_production_email_env(monkeypatch)
    for key, value in override.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError, match=expected):
        Settings()


def test_development_boots_without_email_but_reports_it_unconfigured(monkeypatch) -> None:
    from app.shared.email import email_delivery_configured
    from app.shared.config import settings as settings_module

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("EMAIL_PROVIDER", "none")
    settings_module.get_settings.cache_clear()
    try:
        assert Settings().email_provider == "none"
        assert email_delivery_configured() is False
    finally:
        settings_module.get_settings.cache_clear()


def test_insecure_sso_issuers_are_refused_in_production() -> None:
    """`SSO_ALLOW_INSECURE_ISSUERS` exists for a local Keycloak; it must never be
    honoured by a production process. (The validator is exercised directly so the
    test does not depend on the unrelated production KMS/email requirements.)"""
    production = Settings.model_construct(environment="production", sso_allow_insecure_issuers=True)
    with pytest.raises(ValueError, match="SSO_ALLOW_INSECURE_ISSUERS"):
        production._refuse_insecure_sso_in_production()
    # the same switch is fine outside production, and production without it is fine
    Settings.model_construct(environment="development", sso_allow_insecure_issuers=True)._refuse_insecure_sso_in_production()
    Settings.model_construct(environment="production", sso_allow_insecure_issuers=False)._refuse_insecure_sso_in_production()


def test_sso_defaults_are_conservative(monkeypatch) -> None:
    # ignore any developer .env: these are the code defaults (the two required values come from env)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-test-secret-test-secret")
    monkeypatch.setenv("CONNECTOR_SECRET_MASTER_KEY", "00" * 32)
    monkeypatch.delenv("SSO_ALLOW_INSECURE_ISSUERS", raising=False)
    settings = Settings(_env_file=None)
    assert settings.sso_allow_insecure_issuers is False
    assert settings.sso_transaction_ttl_seconds == 600 and settings.sso_http_timeout_seconds == 10.0
