"""Mock-based tests for OIDC discovery, code exchange, and identity claims.

SIMULATED provider: the HTTP layer is replaced by `_FakeAsyncClient` and tokens are signed with
throw-away keys. These prove EKIP's verification logic, not interoperability with a real
Entra ID / Okta / Auth0 / Google tenant.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt as jose_jwt

from app.core.auth import service as auth_service
from app.core.exceptions import PermissionDeniedError, ServiceUnavailableError
from app.core.tenancy.schemas import SSOConfiguration

ISSUER = "https://idp.example.test"
NONCE = "test-nonce-value"
TOKEN_ENDPOINT = "https://idp.example.test/token"
JWKS_URI = "https://idp.example.test/keys"


class _FakeResponse:
    def __init__(
        self, payload=None, *, status_code: int = 200, json_error: Exception | None = None
    ):
        self.payload = payload
        self.status_code = status_code
        self.json_error = json_error

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://idp.example.test")
            response = httpx.Response(self.status_code, request=request)
            response.raise_for_status()

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


def _patch_http(monkeypatch, *, get_responses, post_response=None):
    calls: list[tuple[str, str, dict | None]] = []

    class _FakeAsyncClient:
        def __init__(self, **kwargs):
            self.timeout = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            calls.append(("GET", url, None))
            if url not in get_responses:
                raise AssertionError(f"Unexpected GET {url}")
            return get_responses[url]

        async def post(self, url, *, data):
            calls.append(("POST", url, data))
            if url != TOKEN_ENDPOINT or post_response is None:
                raise AssertionError(f"Unexpected POST {url}")
            return post_response

    monkeypatch.setattr(auth_service.httpx, "AsyncClient", _FakeAsyncClient)
    return calls


@pytest.fixture(autouse=True)
def _clear_discovery_cache(monkeypatch):
    auth_service._discovery_cache.clear()

    async def _no_dns(url: str) -> None:  # the fake hosts do not resolve; SSRF has its own tests
        return None

    monkeypatch.setattr(auth_service, "_assert_outbound_url_safe", _no_dns)
    yield
    auth_service._discovery_cache.clear()


@pytest.fixture
def sso_config() -> SSOConfiguration:
    now = datetime.now(UTC)
    return SSOConfiguration(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        provider="okta",
        protocol="oidc",
        issuer_url=ISSUER,
        client_id="test-client-id",
        client_secret_ref="encrypted-envelope",
        created_at=now,
        updated_at=now,
    )


def _b64url_int(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _new_signing_key():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": "test-kid",
        "use": "sig",
        "alg": "RS256",
        "n": _b64url_int(numbers.n),
        "e": _b64url_int(numbers.e),
    }
    return private_key, jwk


def _valid_claims(**overrides):
    now = datetime.now(UTC)
    claims = {
        "iss": ISSUER,
        "nonce": NONCE,
        "sub": "provider-user-123",
        "aud": "test-client-id",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "email": "person@example.test",
        "email_verified": True,
        "name": "Test Person",
        "groups": ["engineering", "operators"],
    }
    claims.update(overrides)
    return claims


async def _exchange_signed_token(
    monkeypatch, sso_config, *, claims=None, signing_key=None, jwks=None, token_payload=None,
    nonce=NONCE, algorithm="RS256", headers=None,
):
    private_key, signing_jwk = signing_key or _new_signing_key()
    id_token = jose_jwt.encode(
        claims if claims is not None else _valid_claims(),
        private_key,
        algorithm=algorithm,
        headers=headers or {"kid": "test-kid"},
    )
    auth_service._discovery_cache[ISSUER] = (
        {"issuer": ISSUER, "token_endpoint": TOKEN_ENDPOINT, "jwks_uri": JWKS_URI},
        datetime.now(UTC),
    )
    calls = _patch_http(
        monkeypatch,
        get_responses={JWKS_URI: _FakeResponse(jwks or {"keys": [signing_jwk]})},
        post_response=_FakeResponse(token_payload or {"id_token": id_token}),
    )

    async def fake_resolve_client_secret(reference):
        assert reference == "encrypted-envelope"
        return "decrypted-test-secret"

    monkeypatch.setattr(auth_service, "_resolve_client_secret", fake_resolve_client_secret)
    result = await auth_service._exchange_code_for_claims(
        sso_config,
        code="authorization-code",
        code_verifier="pkce-verifier",
        redirect_uri="https://app.example.test/callback",
        expected_nonce=nonce,
    )
    return result, calls


@pytest.mark.asyncio
async def test_discovery_fetches_and_caches_document(monkeypatch) -> None:
    expected = {"issuer": ISSUER, "authorization_endpoint": "https://idp.example.test/authorize"}
    calls = _patch_http(
        monkeypatch,
        get_responses={f"{ISSUER}/.well-known/openid-configuration": _FakeResponse(expected)},
    )

    first = await auth_service._get_discovery_document(ISSUER)
    second = await auth_service._get_discovery_document(ISSUER)

    assert first == expected
    assert second == expected
    assert calls == [("GET", f"{ISSUER}/.well-known/openid-configuration", None)]


@pytest.mark.asyncio
async def test_expired_discovery_cache_entry_is_refetched(monkeypatch) -> None:
    discovery_url = f"{ISSUER}/.well-known/openid-configuration"
    refreshed = {"issuer": ISSUER, "authorization_endpoint": "https://idp.example.test/new-authorize"}
    auth_service._discovery_cache[ISSUER] = (
        {"issuer": ISSUER, "authorization_endpoint": "https://idp.example.test/old-authorize"},
        datetime.now(UTC) - auth_service._DISCOVERY_CACHE_TTL - timedelta(seconds=1),
    )
    calls = _patch_http(
        monkeypatch, get_responses={discovery_url: _FakeResponse(refreshed)}
    )

    result = await auth_service._get_discovery_document(ISSUER)

    assert result == refreshed
    assert calls == [("GET", discovery_url, None)]


@pytest.mark.asyncio
async def test_discovery_http_error_is_not_cached(monkeypatch) -> None:
    discovery_url = f"{ISSUER}/.well-known/openid-configuration"
    calls = _patch_http(monkeypatch, get_responses={discovery_url: _FakeResponse(status_code=503)})

    with pytest.raises(ServiceUnavailableError) as exc_info:
        await auth_service._get_discovery_document(ISSUER)
    assert exc_info.value.error_code == "auth.idp_unreachable"
    assert ISSUER not in auth_service._discovery_cache
    assert calls[0][1] == discovery_url


@pytest.mark.asyncio
async def test_malformed_discovery_json_is_not_cached(monkeypatch) -> None:
    discovery_url = f"{ISSUER}/.well-known/openid-configuration"
    calls = _patch_http(
        monkeypatch,
        get_responses={
            discovery_url: _FakeResponse(json_error=json.JSONDecodeError("bad json", "{", 0))
        },
    )

    with pytest.raises(ServiceUnavailableError) as exc_info:
        await auth_service._get_discovery_document(ISSUER)
    assert exc_info.value.error_code == "auth.idp_unreachable"
    assert ISSUER not in auth_service._discovery_cache
    assert calls[0][0] == "GET"


@pytest.mark.asyncio
async def test_missing_authorization_endpoint_fails_closed(monkeypatch) -> None:
    auth_service._discovery_cache[ISSUER] = (
        {"issuer": ISSUER, "token_endpoint": TOKEN_ENDPOINT},
        datetime.now(UTC),
    )

    with pytest.raises(ServiceUnavailableError) as exc_info:
        await auth_service._discover_authorization_endpoint(ISSUER)
    assert exc_info.value.error_code == "auth.idp_unreachable"


@pytest.mark.asyncio
async def test_exchange_verifies_signature_and_extracts_standard_groups(
    monkeypatch, sso_config
) -> None:
    result, calls = await _exchange_signed_token(monkeypatch, sso_config)

    assert result.sub == "provider-user-123"
    assert result.email == "person@example.test"
    assert result.groups == ("engineering", "operators")
    post = next(call for call in calls if call[0] == "POST")
    assert post[2] == {
        "grant_type": "authorization_code",
        "code": "authorization-code",
        "redirect_uri": "https://app.example.test/callback",
        "client_id": "test-client-id",
        "client_secret": "decrypted-test-secret",
        "code_verifier": "pkce-verifier",
    }


@pytest.mark.asyncio
async def test_exchange_rejects_token_http_error(monkeypatch, sso_config) -> None:
    auth_service._discovery_cache[ISSUER] = (
        {"issuer": ISSUER, "token_endpoint": TOKEN_ENDPOINT, "jwks_uri": JWKS_URI},
        datetime.now(UTC),
    )
    _patch_http(monkeypatch, get_responses={}, post_response=_FakeResponse(status_code=401))
    monkeypatch.setattr(auth_service, "_resolve_client_secret", _fake_secret)

    with pytest.raises(PermissionDeniedError) as exc_info:
        await auth_service._exchange_code_for_claims(
            sso_config,
            code="code",
            code_verifier="verifier",
            redirect_uri="https://app.example.test/cb",
            expected_nonce=NONCE,
        )
    # a 4xx from the token endpoint = the provider refused the code
    assert exc_info.value.error_code == "auth.idp_exchange_failed"


async def _fake_secret(reference: str) -> str:
    assert reference == "encrypted-envelope"
    return "decrypted-test-secret"


@pytest.mark.asyncio
async def test_exchange_rejects_malformed_token_response(monkeypatch, sso_config) -> None:
    auth_service._discovery_cache[ISSUER] = (
        {"issuer": ISSUER, "token_endpoint": TOKEN_ENDPOINT, "jwks_uri": JWKS_URI},
        datetime.now(UTC),
    )
    _patch_http(
        monkeypatch,
        get_responses={},
        post_response=_FakeResponse(json_error=json.JSONDecodeError("bad json", "{", 0)),
    )
    monkeypatch.setattr(auth_service, "_resolve_client_secret", _fake_secret)

    with pytest.raises(ServiceUnavailableError):
        await auth_service._exchange_code_for_claims(
            sso_config,
            code="code",
            code_verifier="verifier",
            redirect_uri="https://app.example.test/cb",
            expected_nonce=NONCE,
        )


@pytest.mark.asyncio
async def test_exchange_rejects_missing_id_token(monkeypatch, sso_config) -> None:
    auth_service._discovery_cache[ISSUER] = (
        {"issuer": ISSUER, "token_endpoint": TOKEN_ENDPOINT, "jwks_uri": JWKS_URI},
        datetime.now(UTC),
    )
    _patch_http(
        monkeypatch, get_responses={}, post_response=_FakeResponse({"access_token": "opaque"})
    )
    monkeypatch.setattr(auth_service, "_resolve_client_secret", _fake_secret)

    with pytest.raises(PermissionDeniedError, match="did not include an ID token"):
        await auth_service._exchange_code_for_claims(
            sso_config,
            code="code",
            code_verifier="verifier",
            redirect_uri="https://app.example.test/cb",
            expected_nonce=NONCE,
        )


@pytest.mark.asyncio
async def test_exchange_rejects_malformed_id_token(monkeypatch, sso_config) -> None:
    auth_service._discovery_cache[ISSUER] = (
        {"issuer": ISSUER, "token_endpoint": TOKEN_ENDPOINT, "jwks_uri": JWKS_URI},
        datetime.now(UTC),
    )
    _patch_http(
        monkeypatch,
        get_responses={JWKS_URI: _FakeResponse({"keys": []})},
        post_response=_FakeResponse({"id_token": "not-a-jwt"}),
    )
    monkeypatch.setattr(auth_service, "_resolve_client_secret", _fake_secret)

    with pytest.raises(PermissionDeniedError, match="malformed ID token"):
        await auth_service._exchange_code_for_claims(
            sso_config,
            code="code",
            code_verifier="verifier",
            redirect_uri="https://app.example.test/cb",
            expected_nonce=NONCE,
        )


@pytest.mark.asyncio
async def test_exchange_rejects_unknown_kid(monkeypatch, sso_config) -> None:
    _, signing_jwk = _new_signing_key()
    signing_jwk["kid"] = "different-kid"
    auth_service._discovery_cache[ISSUER] = (
        {"issuer": ISSUER, "token_endpoint": TOKEN_ENDPOINT, "jwks_uri": JWKS_URI},
        datetime.now(UTC),
    )
    private_key, _ = _new_signing_key()
    id_token = jose_jwt.encode(
        _valid_claims(), private_key, algorithm="RS256", headers={"kid": "missing-kid"}
    )
    _patch_http(
        monkeypatch,
        get_responses={JWKS_URI: _FakeResponse({"keys": [signing_jwk]})},
        post_response=_FakeResponse({"id_token": id_token}),
    )
    monkeypatch.setattr(auth_service, "_resolve_client_secret", _fake_secret)

    with pytest.raises(PermissionDeniedError, match="matching signing key"):
        await auth_service._exchange_code_for_claims(
            sso_config,
            code="code",
            code_verifier="verifier",
            redirect_uri="https://app.example.test/cb",
            expected_nonce=NONCE,
        )


@pytest.mark.asyncio
async def test_exchange_rejects_invalid_signature_with_matching_kid(
    monkeypatch, sso_config
) -> None:
    signing_key, _ = _new_signing_key()
    _, unrelated_jwk = _new_signing_key()
    unrelated_jwk["kid"] = "test-kid"
    id_token = jose_jwt.encode(
        _valid_claims(), signing_key, algorithm="RS256", headers={"kid": "test-kid"}
    )
    auth_service._discovery_cache[ISSUER] = (
        {"issuer": ISSUER, "token_endpoint": TOKEN_ENDPOINT, "jwks_uri": JWKS_URI},
        datetime.now(UTC),
    )
    _patch_http(
        monkeypatch,
        get_responses={JWKS_URI: _FakeResponse({"keys": [unrelated_jwk]})},
        post_response=_FakeResponse({"id_token": id_token}),
    )
    monkeypatch.setattr(auth_service, "_resolve_client_secret", _fake_secret)

    with pytest.raises(PermissionDeniedError, match="signature or claims verification failed"):
        await auth_service._exchange_code_for_claims(
            sso_config,
            code="code",
            code_verifier="verifier",
            redirect_uri="https://app.example.test/cb",
            expected_nonce=NONCE,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim_changes",
    [
        {"exp": int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())},
        {"iss": "https://attacker.example.test"},
        {"aud": "another-client"},
    ],
    ids=["expired", "wrong-issuer", "wrong-audience"],
)
async def test_exchange_rejects_invalid_standard_claims(
    monkeypatch, sso_config, claim_changes
) -> None:
    with pytest.raises(PermissionDeniedError, match="signature or claims verification failed"):
        await _exchange_signed_token(monkeypatch, sso_config, claims=_valid_claims(**claim_changes))


@pytest.mark.asyncio
async def test_exchange_rejects_missing_email_claim(monkeypatch, sso_config) -> None:
    claims = _valid_claims()
    del claims["email"]
    with pytest.raises(PermissionDeniedError, match="did not provide an email claim"):
        await _exchange_signed_token(monkeypatch, sso_config, claims=claims)


@pytest.mark.asyncio
async def test_exchange_rejects_missing_issued_at_claim(monkeypatch, sso_config) -> None:
    """REVERSAL of `..._currently_accepts_missing_issued_at_claim`: OIDC ID tokens
    require `iat`, and the decoder now enforces it."""
    claims = _valid_claims()
    del claims["iat"]

    with pytest.raises(PermissionDeniedError, match="signature or claims verification failed"):
        await _exchange_signed_token(monkeypatch, sso_config, claims=claims)


@pytest.mark.asyncio
async def test_exchange_rejects_email_the_idp_marked_unverified(monkeypatch, sso_config) -> None:
    """REVERSAL of `test_exchange_currently_trusts_email_without_verified_email_claim`,
    which pinned the vulnerability: an ID token with `email_verified=False` used
    to be accepted and its email trusted for invitation matching, domain rules
    and account linking. It must now be rejected outright."""
    claims = _valid_claims(email_verified=False)

    with pytest.raises(PermissionDeniedError) as exc_info:
        await _exchange_signed_token(monkeypatch, sso_config, claims=claims)

    assert exc_info.value.error_code == "auth.idp_email_not_verified"


@pytest.mark.asyncio
async def test_exchange_rejects_missing_email_verified_claim(monkeypatch, sso_config) -> None:
    """A missing claim is NOT interpreted as true."""
    claims = _valid_claims()
    del claims["email_verified"]

    with pytest.raises(PermissionDeniedError) as exc_info:
        await _exchange_signed_token(monkeypatch, sso_config, claims=claims)

    assert exc_info.value.error_code == "auth.idp_email_not_verified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_value",
    ["true", "True", "1", 1, "yes", None, [], {}, 0],
    ids=["str-true", "str-True", "str-1", "int-1", "str-yes", "null", "list", "dict", "int-0"],
)
async def test_exchange_rejects_non_boolean_email_verified_claim(
    monkeypatch, sso_config, bad_value
) -> None:
    """Strict typing: only a JSON boolean `true` is evidence. `"true"` (some
    IdPs / misconfigured mappers emit strings) and `1` are rejected, not coerced."""
    claims = _valid_claims(email_verified=bad_value)

    with pytest.raises(PermissionDeniedError) as exc_info:
        await _exchange_signed_token(monkeypatch, sso_config, claims=claims)

    assert exc_info.value.error_code == "auth.idp_email_not_verified"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_email", [None, "", "   ", 123, ["a@b.test"]])
async def test_exchange_rejects_missing_or_non_string_email(monkeypatch, sso_config, bad_email) -> None:
    claims = _valid_claims(email=bad_email)

    with pytest.raises(PermissionDeniedError) as exc_info:
        await _exchange_signed_token(monkeypatch, sso_config, claims=claims)

    assert exc_info.value.error_code == "auth.idp_response_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["okta", "auth0", "google_workspace"])
async def test_exchange_accepts_standard_email_verified_true_for_standard_oidc_providers(
    monkeypatch, sso_config, provider
) -> None:
    config = sso_config.model_copy(update={"provider": provider})

    result, _ = await _exchange_signed_token(monkeypatch, config)

    assert result.email_verified is True
    assert result.email_verification_source == "email_verified"
    assert result.email == "person@example.test"


@pytest.mark.asyncio
async def test_entra_without_any_ownership_evidence_is_rejected(monkeypatch, sso_config) -> None:
    """Microsoft Entra ID does not emit `email_verified` by default and its
    `email` claim is mutable/unverified: with neither `email_verified` nor
    `xms_edov` the address must not be trusted."""
    config = sso_config.model_copy(update={"provider": "entra_id"})
    claims = _valid_claims()
    del claims["email_verified"]

    with pytest.raises(PermissionDeniedError) as exc_info:
        await _exchange_signed_token(monkeypatch, config, claims=claims)

    assert exc_info.value.error_code == "auth.idp_email_not_verified"


@pytest.mark.asyncio
async def test_entra_accepts_xms_edov_true_as_ownership_evidence(monkeypatch, sso_config) -> None:
    config = sso_config.model_copy(update={"provider": "entra_id"})
    claims = _valid_claims(xms_edov=True)
    del claims["email_verified"]

    result, _ = await _exchange_signed_token(monkeypatch, config, claims=claims)

    assert result.email_verified is True
    assert result.email_verification_source == "xms_edov"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", [False, "true", 1, None])
async def test_entra_xms_edov_must_be_a_true_boolean(monkeypatch, sso_config, bad_value) -> None:
    config = sso_config.model_copy(update={"provider": "entra_id"})
    claims = _valid_claims(xms_edov=bad_value)
    del claims["email_verified"]

    with pytest.raises(PermissionDeniedError) as exc_info:
        await _exchange_signed_token(monkeypatch, config, claims=claims)

    assert exc_info.value.error_code == "auth.idp_email_not_verified"


@pytest.mark.asyncio
async def test_xms_edov_is_only_honoured_for_entra(monkeypatch, sso_config) -> None:
    """`xms_edov` is a Microsoft claim: another provider asserting it (or an
    attacker-influenced custom claim of that name) is not evidence."""
    claims = _valid_claims(xms_edov=True)
    del claims["email_verified"]

    with pytest.raises(PermissionDeniedError) as exc_info:
        await _exchange_signed_token(monkeypatch, sso_config, claims=claims)  # provider=okta

    assert exc_info.value.error_code == "auth.idp_email_not_verified"


@pytest.mark.asyncio
async def test_unverified_email_still_fails_before_it_can_reach_provisioning(monkeypatch, sso_config) -> None:
    """Signature/issuer/audience/expiry checks are unchanged AND still come first:
    a forged token with email_verified=true is rejected by signature verification,
    not accepted on the strength of its claim."""
    other_key = _new_signing_key()
    claims = _valid_claims(email_verified=True)
    with pytest.raises(PermissionDeniedError, match="signature or claims verification failed"):
        await _exchange_signed_token(
            monkeypatch,
            sso_config,
            claims=claims,
            signing_key=other_key,
            jwks={"keys": [_new_signing_key()[1]]},
        )


@pytest.mark.asyncio
async def test_exchange_rejects_missing_subject_cleanly(monkeypatch, sso_config) -> None:
    """Was an unclassified `KeyError` (HTTP 500); now a classified rejection."""
    claims = _valid_claims()
    del claims["sub"]

    with pytest.raises(PermissionDeniedError, match="signature or claims verification failed"):
        await _exchange_signed_token(monkeypatch, sso_config, claims=claims)


@pytest.mark.asyncio
async def test_exchange_rejects_malformed_jwks_response(monkeypatch, sso_config) -> None:
    private_key, _ = _new_signing_key()
    id_token = jose_jwt.encode(
        _valid_claims(), private_key, algorithm="RS256", headers={"kid": "test-kid"}
    )
    auth_service._discovery_cache[ISSUER] = (
        {"issuer": ISSUER, "token_endpoint": TOKEN_ENDPOINT, "jwks_uri": JWKS_URI},
        datetime.now(UTC),
    )
    _patch_http(
        monkeypatch,
        get_responses={JWKS_URI: _FakeResponse(status_code=502)},
        post_response=_FakeResponse({"id_token": id_token}),
    )
    monkeypatch.setattr(auth_service, "_resolve_client_secret", _fake_secret)

    with pytest.raises(ServiceUnavailableError):
        await auth_service._exchange_code_for_claims(
            sso_config,
            code="code",
            code_verifier="verifier",
            redirect_uri="https://app.example.test/cb",
            expected_nonce=NONCE,
        )


@pytest.mark.asyncio
async def test_missing_token_endpoint_fails_closed(monkeypatch, sso_config) -> None:
    auth_service._discovery_cache[ISSUER] = (
        {"issuer": ISSUER, "jwks_uri": JWKS_URI},
        datetime.now(UTC),
    )
    monkeypatch.setattr(auth_service, "_resolve_client_secret", _fake_secret)

    with pytest.raises(ServiceUnavailableError):
        await auth_service._exchange_code_for_claims(
            sso_config,
            code="code",
            code_verifier="verifier",
            redirect_uri="https://app.example.test/cb",
            expected_nonce=NONCE,
        )


@pytest.mark.asyncio
async def test_auth0_namespaced_group_claim_is_not_treated_as_standard_groups(
    monkeypatch, sso_config
) -> None:
    claims = _valid_claims()
    del claims["groups"]
    claims["https://example.test/claims/groups"] = ["engineering"]

    result, _ = await _exchange_signed_token(monkeypatch, sso_config, claims=claims)

    assert result.groups == ()
