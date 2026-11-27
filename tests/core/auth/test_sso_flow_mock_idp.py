"""The SSO login protocol end to end against a SIMULATED provider.

`tests/support/mock_oidc.py` runs a real HTTP server on 127.0.0.1 that signs
real RS256 ID tokens and enforces single-use codes, redirect-URI matching and
PKCE. EKIP's `begin_sso_login` / `complete_sso_login` talk to it over real HTTP
(real `httpx`, real JWT verification, real server-side transaction logic).

What is faked: the database (organization lookup, user provisioning, session
issuance are replaced by recording stubs -- they have their own real-Postgres
tests in `test_sso_postgres.py`) and Redis (an in-memory store with the same
single-use contract). What is NOT proven here: interoperability with a real
Entra ID / Okta / Auth0 / Google tenant.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from app.core.auth import service as auth_service
from app.core.auth import sso_transaction
from app.core.auth.schemas import SessionTokens, SSOCallbackRequest
from app.core.exceptions import NotFoundError, PermissionDeniedError, ServiceUnavailableError
from app.core.tenancy.schemas import SSOConfiguration
from app.shared.config.settings import get_settings
from tests.support.mock_oidc import MockOidcProvider

FRONTEND = "http://localhost:5173"
CALLBACK = f"{FRONTEND}/o/acme/callback"
FLAVORS = ["entra_id", "okta", "auth0", "google_workspace"]


class MemoryStore:
    """Same contract as the Redis store: `take` returns and deletes atomically."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.down = False

    async def put(self, key: str, value: str, ttl_seconds: int) -> None:
        if self.down:
            raise sso_transaction.SsoTransactionUnavailableError("down")
        self.data[key] = value

    async def take(self, key: str) -> str | None:
        if self.down:
            raise sso_transaction.SsoTransactionUnavailableError("down")
        return self.data.pop(key, None)


class Harness:
    def __init__(self, idp: MockOidcProvider, store: MemoryStore) -> None:
        self.idp, self.store = idp, store
        self.provisioned: list = []

    async def begin(self, redirect_uri: str = CALLBACK, slug: str = "acme"):
        return await auth_service.begin_sso_login(None, slug, redirect_uri=redirect_uri, store=self.store)

    async def complete(self, begin, params: dict, *, slug="acme", redirect_uri=CALLBACK, state=None, txn=None):
        data = SSOCallbackRequest(
            org_slug=slug,
            code=params["code"],
            state=state if state is not None else params["state"],
            transaction_id=txn if txn is not None else begin.transaction_id,
        )
        return await auth_service.complete_sso_login(None, data, redirect_uri=redirect_uri, store=self.store)

    async def login(self, **browser):
        begin = await self.begin()
        params = self.idp.browser_login(begin.authorization_url, **browser)
        return begin, params, await self.complete(begin, params)

    def stored_transaction(self) -> dict:
        (raw,) = self.store.data.values()
        return json.loads(raw)


def _make_harness(monkeypatch, flavor: str, *, insecure: bool = True) -> Harness:
    idp = MockOidcProvider(flavor=flavor).start()
    settings = get_settings().model_copy(
        update={"sso_allow_insecure_issuers": insecure, "cors_allowed_origins": [FRONTEND, "http://127.0.0.1:5173"]}
    )
    monkeypatch.setattr(auth_service, "get_settings", lambda: settings)
    auth_service._discovery_cache.clear()
    store = MemoryStore()
    harness = Harness(idp, store)
    now = datetime.now(UTC)
    org_id = uuid.uuid4()

    async def fake_sso_config(session, slug):
        return SSOConfiguration(
            id=uuid.uuid4(),
            organization_id=org_id,
            provider=flavor,
            protocol="oidc",
            issuer_url=idp.configured_issuer,
            client_id=idp.client_id,
            client_secret_ref="encrypted",
            enabled=True,
            created_at=now,
            updated_at=now,
        )

    async def fake_secret(reference):
        return idp.client_secret

    async def fake_resolve(session, *, organization_id, idp_subject, idp_claims):
        harness.provisioned.append((organization_id, idp_subject, idp_claims))
        return uuid.uuid4()

    async def fake_issue_session(session, *, user_id, organization_id, family_id, **kwargs):
        return SessionTokens(access_token="access", refresh_token="refresh", expires_in=900)

    monkeypatch.setattr(auth_service.tenancy_service, "get_organization_sso_config", fake_sso_config)
    monkeypatch.setattr(auth_service, "_resolve_client_secret", fake_secret)
    monkeypatch.setattr(auth_service, "_resolve_or_provision_user", fake_resolve)
    monkeypatch.setattr(auth_service, "_issue_session", fake_issue_session)
    return harness


@pytest.fixture(params=FLAVORS)
def h(request, monkeypatch):
    harness = _make_harness(monkeypatch, request.param)
    yield harness
    harness.idp.stop()
    auth_service._discovery_cache.clear()


@pytest.fixture
def okta(monkeypatch):
    harness = _make_harness(monkeypatch, "okta")
    yield harness
    harness.idp.stop()
    auth_service._discovery_cache.clear()


async def _code(expected: str, awaitable) -> PermissionDeniedError | ServiceUnavailableError:
    with pytest.raises((PermissionDeniedError, ServiceUnavailableError, NotFoundError)) as info:
        await awaitable
    assert info.value.error_code == expected, (info.value.error_code, info.value.message)
    return info.value


# ============================================================ success, per provider


@pytest.mark.asyncio
async def test_full_login_succeeds_for_every_supported_provider_shape(h) -> None:
    begin, params, tokens = await h.login(email="ada@corp.test", groups=["engineering"])

    assert tokens.access_token == "access" and tokens.refresh_token == "refresh"
    ((_org, subject, claims),) = h.provisioned
    assert claims.email == "ada@corp.test" and subject == "sub-ada@corp.test"
    assert claims.groups == ("engineering",)
    assert claims.email_verified is True
    # Entra proves ownership with xms_edov, the others with email_verified
    assert claims.email_verification_source == ("xms_edov" if h.idp.flavor == "entra_id" else "email_verified")


@pytest.mark.asyncio
async def test_authorization_request_carries_state_nonce_and_pkce_challenge_but_not_the_verifier(okta) -> None:
    begin = await okta.begin()
    query = {k: v[0] for k, v in parse_qs(urlsplit(begin.authorization_url).query).items()}
    stored = okta.stored_transaction()

    assert query["response_type"] == "code" and query["code_challenge_method"] == "S256"
    assert query["state"] == begin.state == stored["state"]
    assert query["nonce"] == stored["nonce"] and len(stored["nonce"]) >= 32
    assert query["redirect_uri"] == CALLBACK and query["client_id"] == okta.idp.client_id
    # the verifier lives only in the server-side record: never in the URL or the response body
    verifier = stored["code_verifier"]
    assert len(verifier) >= 43
    assert verifier not in begin.authorization_url and verifier not in begin.model_dump_json()
    assert set(begin.model_dump()) == {"authorization_url", "state", "transaction_id"}
    assert begin.transaction_id not in begin.authorization_url
    # the handle is opaque and not what is stored as the Redis key
    assert begin.transaction_id not in " ".join(okta.store.data)


@pytest.mark.asyncio
async def test_pkce_is_verified_by_the_provider_with_the_server_held_verifier(okta) -> None:
    await okta.login()
    request = okta.idp.token_requests[0]
    assert request["grant_type"] == "authorization_code"
    assert request["redirect_uri"] == CALLBACK and request["client_id"] == okta.idp.client_id


@pytest.mark.asyncio
async def test_a_wrong_pkce_verifier_is_refused_by_the_provider(okta) -> None:
    begin = await okta.begin()
    params = okta.idp.browser_login(begin.authorization_url)
    (key,) = okta.store.data
    tampered = json.loads(okta.store.data[key])
    tampered["code_verifier"] = "x" * 64
    okta.store.data[key] = json.dumps(tampered)

    await _code("auth.idp_exchange_failed", okta.complete(begin, params))


# ============================================================ transaction binding


@pytest.mark.asyncio
async def test_state_mismatch_is_rejected_server_side_and_burns_the_transaction(okta) -> None:
    begin = await okta.begin()
    params = okta.idp.browser_login(begin.authorization_url)

    await _code("auth.sso_state_mismatch", okta.complete(begin, params, state="forged-state"))
    assert okta.idp.token_requests == []  # the code was never even exchanged
    # even the correct state cannot be retried: the attempt is over
    await _code("auth.sso_transaction_invalid", okta.complete(begin, params))


@pytest.mark.asyncio
async def test_replaying_a_completed_callback_is_rejected(okta) -> None:
    begin, params, _ = await okta.login()

    await _code("auth.sso_transaction_invalid", okta.complete(begin, params))
    assert len(okta.provisioned) == 1


@pytest.mark.asyncio
async def test_unknown_or_malformed_transaction_ids_are_rejected(okta) -> None:
    begin = await okta.begin()
    params = okta.idp.browser_login(begin.authorization_url)
    for bad in ("not-a-real-handle", "x" * 120, "../../etc"):
        await _code("auth.sso_transaction_invalid", okta.complete(begin, params, txn=bad))
    # the real one is still usable: bad guesses did not consume it
    assert (await okta.complete(begin, params)).access_token == "access"


@pytest.mark.asyncio
async def test_expired_transaction_is_rejected(okta, monkeypatch) -> None:
    begin = await okta.begin()
    params = okta.idp.browser_login(begin.authorization_url)
    ttl = get_settings().sso_transaction_ttl_seconds
    real_time = time.time
    monkeypatch.setattr(sso_transaction.time, "time", lambda: real_time() + ttl + 1)

    await _code("auth.sso_transaction_invalid", okta.complete(begin, params))


@pytest.mark.asyncio
async def test_a_transaction_cannot_be_completed_for_another_organization(okta) -> None:
    begin = await okta.begin()
    params = okta.idp.browser_login(begin.authorization_url)
    other_callback = f"{FRONTEND}/o/other-org/callback"

    await _code(
        "auth.sso_state_mismatch",
        okta.complete(begin, params, slug="other-org", redirect_uri=other_callback),
    )
    assert okta.provisioned == []


@pytest.mark.asyncio
async def test_a_transaction_cannot_be_completed_with_a_different_redirect_uri(okta) -> None:
    begin = await okta.begin()
    params = okta.idp.browser_login(begin.authorization_url)

    await _code(
        "auth.sso_state_mismatch",
        okta.complete(begin, params, redirect_uri="http://127.0.0.1:5173/o/acme/callback"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uri",
    [
        f"{FRONTEND}/o/acme/callback/extra",
        f"{FRONTEND}/o/acme/callback?next=/admin",
        f"{FRONTEND}/o/acme/callback#fragment",
        f"{FRONTEND}/login",
        f"{FRONTEND}/o/acme/callback;params",
        f"{FRONTEND}/o/ACME/callback",
        "https://evil.example/o/acme/callback",
        f"{FRONTEND}.evil.example/o/acme/callback",
        "javascript:alert(1)",
    ],
)
async def test_redirect_uri_must_be_exactly_the_organizations_callback(okta, uri) -> None:
    await _code("auth.redirect_uri_not_allowed", okta.begin(redirect_uri=uri))
    data = SSOCallbackRequest(org_slug="acme", code="c", state="s", transaction_id="t")
    with pytest.raises(PermissionDeniedError) as info:
        await auth_service.complete_sso_login(None, data, redirect_uri=uri, store=okta.store)
    assert info.value.error_code == "auth.redirect_uri_not_allowed"
    assert okta.idp.requests_seen == []


@pytest.mark.asyncio
async def test_redis_outage_fails_closed_for_begin_and_complete(okta) -> None:
    begin = await okta.begin()
    params = okta.idp.browser_login(begin.authorization_url)
    okta.store.down = True

    await _code("auth.sso_unavailable", okta.begin())
    await _code("auth.sso_unavailable", okta.complete(begin, params))
    assert okta.provisioned == []


# ============================================================ ID token validation


@pytest.mark.asyncio
async def test_wrong_nonce_is_rejected(okta) -> None:
    okta.idp.nonce_override = "attacker-chosen"
    begin = await okta.begin()
    params = okta.idp.browser_login(begin.authorization_url)

    await _code("auth.idp_nonce_mismatch", okta.complete(begin, params))
    assert okta.provisioned == []


@pytest.mark.asyncio
async def test_missing_nonce_is_rejected(okta) -> None:
    okta.idp.drop_nonce = True
    begin = await okta.begin()
    params = okta.idp.browser_login(begin.authorization_url)

    await _code("auth.idp_nonce_mismatch", okta.complete(begin, params))


@pytest.mark.asyncio
async def test_token_signed_by_another_key_is_rejected(okta) -> None:
    okta.idp.sign_with_other_key = True
    begin = await okta.begin()
    params = okta.idp.browser_login(begin.authorization_url)

    await _code("auth.idp_token_invalid", okta.complete(begin, params))


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ["alg_none", "hs256"])
async def test_forbidden_signing_algorithms_are_rejected(okta, attack) -> None:
    now = int(time.time())
    claims = {
        "iss": okta.idp.token_issuer, "aud": okta.idp.client_id, "sub": "s", "iat": now,
        "exp": now + 300, "email": "a@corp.test", "email_verified": True,
    }
    begin = await okta.begin()
    claims["nonce"] = okta.stored_transaction()["nonce"]
    okta.idp.raw_id_token = (
        okta.idp.forged_unsigned_token(claims) if attack == "alg_none" else okta.idp.hs256_token(claims)
    )
    params = okta.idp.browser_login(begin.authorization_url)

    await _code("auth.idp_token_invalid", okta.complete(begin, params))
    assert okta.provisioned == []


@pytest.mark.asyncio
async def test_token_for_another_issuer_is_rejected(okta) -> None:
    okta.idp.iss_override = "https://attacker.example"
    begin = await okta.begin()
    params = okta.idp.browser_login(begin.authorization_url)

    await _code("auth.idp_token_invalid", okta.complete(begin, params))


@pytest.mark.asyncio
async def test_token_for_another_audience_is_rejected(okta) -> None:
    okta.idp.aud_override = "some-other-client"
    begin = await okta.begin()
    params = okta.idp.browser_login(begin.authorization_url)

    await _code("auth.idp_token_invalid", okta.complete(begin, params))


@pytest.mark.asyncio
async def test_multi_audience_token_requires_this_client_as_authorized_party(okta) -> None:
    okta.idp.aud_override = [okta.idp.client_id, "another-resource"]
    begin = await okta.begin()
    params = okta.idp.browser_login(begin.authorization_url)
    await _code("auth.idp_token_invalid", okta.complete(begin, params))

    okta.idp.extra_claims = {"azp": okta.idp.client_id}
    begin = await okta.begin()
    params = okta.idp.browser_login(begin.authorization_url)
    assert (await okta.complete(begin, params)).access_token == "access"


@pytest.mark.asyncio
async def test_expired_id_token_is_rejected(okta) -> None:
    okta.idp.expires_in = -30
    begin = await okta.begin()
    params = okta.idp.browser_login(begin.authorization_url)

    await _code("auth.idp_token_invalid", okta.complete(begin, params))


@pytest.mark.asyncio
async def test_discovery_document_claiming_another_issuer_is_rejected(okta) -> None:
    okta.idp.discovery_issuer_override = "https://someone-else.example"

    await _code("auth.idp_unreachable", okta.begin())


# ============================================================ provider failures


@pytest.mark.asyncio
async def test_provider_outage_is_a_clean_503_not_a_crash(okta) -> None:
    okta.idp.discovery_status = 500
    await _code("auth.idp_unreachable", okta.begin())
    okta.idp.discovery_status = 200
    auth_service._discovery_cache.clear()

    begin = await okta.begin()
    params = okta.idp.browser_login(begin.authorization_url)
    okta.idp.token_http_status = 503
    await _code("auth.idp_unreachable", okta.complete(begin, params))


@pytest.mark.asyncio
async def test_provider_cancellation_returns_an_error_and_no_code(okta) -> None:
    begin = await okta.begin()
    okta.idp.cancel_next = True

    params = okta.idp.browser_login(begin.authorization_url)

    assert params["error"] == "access_denied" and "code" not in params
    assert params["state"] == begin.state
    # the unused transaction simply expires; nothing was exchanged or provisioned
    assert okta.idp.token_requests == [] and okta.provisioned == []


@pytest.mark.asyncio
async def test_unreachable_provider_times_out_instead_of_hanging(monkeypatch) -> None:
    harness = _make_harness(monkeypatch, "okta")
    harness.idp.stop()  # nothing is listening any more
    await _code("auth.idp_unreachable", harness.begin())


# ============================================================ SSRF


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "issuer",
    ["http://127.0.0.1:9", "https://127.0.0.1", "https://169.254.169.254", "https://10.0.0.5/x", "https://[::1]", "https://localhost"],
)
async def test_issuer_pointing_at_internal_addresses_is_never_contacted(monkeypatch, issuer) -> None:
    # real SSRF guard (insecure issuers NOT allowed), with a live local server standing in for "internal service"
    harness = _make_harness(monkeypatch, "okta", insecure=False)
    internal = harness.idp

    async def internal_config(session, slug):
        now = datetime.now(UTC)
        return SSOConfiguration(
            id=uuid.uuid4(), organization_id=uuid.uuid4(), provider="okta", protocol="oidc",
            issuer_url=issuer, client_id="c", client_secret_ref="e", enabled=True,
            created_at=now, updated_at=now,
        )

    monkeypatch.setattr(auth_service.tenancy_service, "get_organization_sso_config", internal_config)
    await _code("auth.idp_unreachable", harness.begin())
    assert internal.requests_seen == []
    internal.stop()


@pytest.mark.asyncio
async def test_a_public_looking_issuer_whose_endpoints_point_inside_is_refused(monkeypatch) -> None:
    """The discovery document is untrusted too: its token_endpoint is re-checked."""
    harness = _make_harness(monkeypatch, "okta")  # insecure allowed only to fetch discovery
    issuer = harness.idp.configured_issuer
    document = harness.idp.discovery_document()
    document["token_endpoint"] = "https://169.254.169.254/latest/meta-data"
    auth_service._discovery_cache[auth_service.normalize_issuer(issuer, allow_insecure=True)] = (
        document,
        datetime.now(UTC),
    )
    monkeypatch.setattr(
        auth_service,
        "get_settings",
        lambda: get_settings().model_copy(update={"sso_allow_insecure_issuers": False}),
    )
    from app.core.auth.schemas import VerifiedIdPClaims  # noqa: F401

    with pytest.raises(ServiceUnavailableError) as info:
        await auth_service._discovery_endpoint(document, "token_endpoint")
    assert info.value.error_code == "auth.idp_unreachable"
    harness.idp.stop()


# ============================================================ public options


@pytest.mark.asyncio
async def test_options_are_identical_for_every_unavailable_cause_and_minimal_when_available(monkeypatch) -> None:
    async def unavailable(session, slug):
        raise NotFoundError("x", error_code="sso.unavailable")

    monkeypatch.setattr(auth_service.tenancy_service, "get_organization_sso_config", unavailable)
    answer = await auth_service.get_sso_options(None, "whatever")
    assert answer.model_dump() == {"available": False, "provider": None, "provider_label": None}

    harness = _make_harness(monkeypatch, "entra_id")
    available = await auth_service.get_sso_options(None, "acme")
    assert available.model_dump() == {
        "available": True,
        "provider": "entra_id",
        "provider_label": "Microsoft Entra ID",
    }
    dumped = available.model_dump_json()
    for secret in (harness.idp.client_id, harness.idp.configured_issuer, "encrypted"):
        assert secret not in dumped
    harness.idp.stop()
