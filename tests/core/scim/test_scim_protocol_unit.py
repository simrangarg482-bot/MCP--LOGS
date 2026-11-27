"""SCIM protocol behaviour that needs no database: payload and PATCH normalisation (including the Entra ID and
Okta variations), filters, resource shapes, discovery documents, token hashing and authentication decisions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.scim import repository, schemas, tokens
from app.core.scim.errors import ScimError

PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


def _patch(*operations):
    return schemas.parse_patch({"schemas": [PATCH], "Operations": list(operations)})


# ---------------------------------------------------------------- booleans


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False), ("True", True), ("False", False), ("true", True), ("false", False), (" FALSE ", False)],
)
def test_active_accepts_json_booleans_and_the_strings_entra_sends(value, expected) -> None:
    assert schemas.parse_bool(value) is expected


@pytest.mark.parametrize("value", [None, 0, 1, "yes", "", [], {}, 1.0])
def test_active_rejects_anything_else(value) -> None:
    with pytest.raises(ScimError) as info:
        schemas.parse_bool(value)
    assert info.value.status == 400 and info.value.scim_type == "invalidValue"


# ---------------------------------------------------------------- PATCH, Entra ID style


def test_entra_replace_active_as_a_capitalised_op_and_a_string_value() -> None:
    fields = _patch({"op": "Replace", "path": "active", "value": "False"})
    assert fields.active is False and fields.provided == {"active"}


def test_entra_pathless_replace_with_an_object_of_attributes_and_unknown_ones_ignored() -> None:
    fields = _patch(
        {
            "op": "Replace",
            "value": {
                "active": "True",
                "displayName": "Ada Lovelace",
                "title": "Engineer",
                "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User:manager": "x",
                "department": "Platform",
            },
        }
    )
    assert fields.active is True and fields.display_name == "Ada Lovelace"
    assert fields.provided == {"active", "displayName"}


def test_entra_enterprise_extension_and_complex_paths_never_fail_a_sync() -> None:
    fields = _patch(
        {"op": "Add", "path": "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User:department", "value": "R&D"},
        {"op": "Replace", "path": "name.givenName", "value": "Ada"},
        {"op": "Replace", "path": 'emails[type eq "work"].value', "value": "a@example.test"},
        {"op": "Replace", "path": "title", "value": "Dr"},
        {"op": "Replace", "path": "somethingTheIdpInvented", "value": 1},
    )
    assert fields.provided == set()


def test_the_last_operation_on_a_field_wins() -> None:
    assert _patch(
        {"op": "replace", "path": "active", "value": False}, {"op": "replace", "path": "active", "value": True}
    ).active is True


# ---------------------------------------------------------------- PATCH, Okta style and refusals


def test_okta_replace_active_as_a_real_boolean() -> None:
    assert _patch({"op": "replace", "path": "active", "value": False}).active is False


def test_username_externalid_and_display_name_are_stored_attributes() -> None:
    fields = _patch(
        {"op": "replace", "path": "userName", "value": "Ada@Example.test"},
        {"op": "add", "path": "externalId", "value": "00u1abc"},
        {"op": "remove", "path": "displayName"},
    )
    assert fields.user_name == "Ada@Example.test" and fields.external_id == "00u1abc"
    assert fields.display_name is None and fields.provided == {"userName", "externalId", "displayName"}


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "replace", "path": "password", "value": "x"},
        {"op": "replace", "path": "groups", "value": []},
        {"op": "remove", "path": "active"},
        {"op": "remove", "path": "userName"},
        {"op": "move", "path": "active", "value": True},
        {"op": "replace", "path": "active", "value": "maybe"},
        {"op": "replace", "value": "not an object"},
        {"op": "replace", "path": "userName", "value": ""},
        {"op": "replace", "path": 5, "value": True},
        "not an operation",
    ],
)
def test_operations_that_would_change_the_access_model_or_are_malformed_are_refused(operation) -> None:
    with pytest.raises(ScimError) as info:
        _patch(operation)
    assert info.value.status == 400


@pytest.mark.parametrize("body", [None, [], "x", {}, {"Operations": []}, {"Operations": "nope"}])
def test_a_patch_without_operations_is_invalid(body) -> None:
    with pytest.raises(ScimError) as info:
        schemas.parse_patch(body)
    assert info.value.status == 400 and info.value.scim_type == "invalidSyntax"


def test_too_many_operations_are_refused() -> None:
    with pytest.raises(ScimError) as info:
        schemas.parse_patch({"Operations": [{"op": "replace", "path": "active", "value": True}] * 101})
    assert info.value.scim_type == "tooMany"


# ---------------------------------------------------------------- POST / PUT payloads


def test_a_user_payload_yields_the_stored_fields_only() -> None:
    fields = schemas.parse_user_payload(
        {
            "schemas": [schemas.USER_SCHEMA],
            "userName": "  ada@example.test ",
            "externalId": "00u1",
            "name": {"givenName": "Ada", "familyName": "Lovelace"},
            "emails": [{"value": "ada@example.test", "primary": True}],
            "active": "True",
            "title": "x",
        },
        require_user_name=True,
    )
    assert (fields.user_name, fields.external_id, fields.display_name, fields.active) == (
        "ada@example.test", "00u1", "Ada Lovelace", True,
    )


@pytest.mark.parametrize(
    "payload",
    [None, [], {"userName": ""}, {"userName": 5}, {"userName": "x" * 400},
     {"userName": "a", "schemas": ["urn:wrong"]}, {"userName": "a", "password": "x"}, {"userName": "a", "active": "nah"}],
)
def test_malformed_or_unsupported_user_payloads_are_refused(payload) -> None:
    with pytest.raises(ScimError) as info:
        schemas.parse_user_payload(payload, require_user_name=True)
    assert info.value.status == 400


def test_a_put_without_username_is_allowed_a_post_without_it_is_not() -> None:
    assert schemas.parse_user_payload({"active": False}, require_user_name=False).active is False
    with pytest.raises(ScimError):
        schemas.parse_user_payload({"active": False}, require_user_name=True)


# ---------------------------------------------------------------- filters and paging


def test_only_username_and_externalid_equality_filters_are_supported() -> None:
    assert schemas.parse_filter('userName eq "Ada@Example.test"') == ("userName", "Ada@Example.test")
    assert schemas.parse_filter('USERNAME EQ "a"') == ("userName", "a")
    assert schemas.parse_filter('externalId eq "00u1"') == ("externalId", "00u1")
    assert schemas.parse_filter(r'userName eq "a\"b"') == ("userName", 'a"b')
    assert schemas.parse_filter(None) is None and schemas.parse_filter("  ") is None


@pytest.mark.parametrize(
    "text",
    ['userName co "a"', 'userName eq "a" and active eq true', 'emails.value eq "a"', "userName eq a", 'userName eq "a" or userName eq "b"', 'active eq "true"'],
)
def test_any_other_filter_is_refused_instead_of_silently_returning_everyone(text) -> None:
    with pytest.raises(ScimError) as info:
        schemas.parse_filter(text)
    assert info.value.scim_type == "invalidFilter"


def test_paging_is_clamped() -> None:
    assert schemas.clamp_paging(None, None) == (1, 100)
    assert schemas.clamp_paging(0, 5) == (1, 5)
    assert schemas.clamp_paging(3, 1000) == (3, 100)
    assert schemas.clamp_paging(1, 0) == (1, 0)
    assert schemas.clamp_paging(1, -4) == (1, 0)


# ---------------------------------------------------------------- resources and discovery


def test_a_user_resource_has_the_scim_shape() -> None:
    ident = SimpleNamespace(
        id=uuid.uuid4(), user_name="ada@example.test", external_id="00u1", display_name="Ada",
        created_at=datetime(2026, 1, 1, tzinfo=UTC), updated_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    resource = schemas.user_resource(ident, "https://api.example/scim/v2", effective_active=False)
    assert resource["schemas"] == [schemas.USER_SCHEMA] and resource["active"] is False
    assert resource["id"] == str(ident.id) and resource["externalId"] == "00u1" and resource["displayName"] == "Ada"
    assert resource["meta"]["resourceType"] == "User"
    assert resource["meta"]["location"] == f"https://api.example/scim/v2/Users/{ident.id}"
    assert "password" not in resource


def test_discovery_documents_describe_what_is_and_is_not_supported() -> None:
    config = schemas.service_provider_config("https://api.example/scim/v2")
    assert config["patch"]["supported"] is True and config["bulk"]["supported"] is False
    assert config["sort"]["supported"] is False and config["etag"]["supported"] is False
    assert config["changePassword"]["supported"] is False
    assert config["authenticationSchemes"][0]["type"] == "oauthbearertoken"
    types = schemas.resource_types("https://x/scim/v2", groups=True)
    assert [t["id"] for t in types] == ["User", "Group"]
    assert [t["id"] for t in schemas.resource_types("https://x/scim/v2", groups=False)] == ["User"]
    assert {a["name"] for a in schemas.user_schema("https://x")["attributes"]} == {"userName", "externalId", "displayName", "active"}


def test_error_bodies_follow_rfc_7644() -> None:
    body = ScimError(409, "dup", "uniqueness").body()
    assert body == {"schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"], "status": "409", "detail": "dup", "scimType": "uniqueness"}
    assert "scimType" not in ScimError(404, "x").body()


# ---------------------------------------------------------------- tokens


def test_tokens_are_opaque_random_prefixed_and_hashed_with_sha256() -> None:
    a, b = tokens.generate_raw_token(), tokens.generate_raw_token()
    assert a != b and a.startswith("scim_") and len(a) > 40
    digest = tokens.hash_token(a)
    assert len(digest) == 64 and digest != a and a not in digest and tokens.hash_token(a) == digest


@pytest.fixture
def fake_lookup(monkeypatch):
    rows: dict[str, tuple] = {}

    async def lookup(session, token_hash):
        return rows.get(token_hash)

    monkeypatch.setattr(repository, "authenticate_token", lookup)
    return rows


@pytest.mark.asyncio
async def test_authentication_resolves_the_organization_from_the_token_alone(fake_lookup) -> None:
    raw = tokens.generate_raw_token()
    org, tid = uuid.uuid4(), uuid.uuid4()
    fake_lookup[tokens.hash_token(raw)] = (tid, org, datetime.now(UTC) + timedelta(days=1), None)
    auth = await tokens.authenticate(None, raw)  # type: ignore[arg-type]
    assert (auth.organization_id, auth.token_id) == (org, tid)


@pytest.mark.asyncio
async def test_every_rejection_is_the_same_401(fake_lookup) -> None:
    now = datetime.now(UTC)
    expired, revoked, good_hash = tokens.generate_raw_token(), tokens.generate_raw_token(), tokens.generate_raw_token()
    fake_lookup[tokens.hash_token(expired)] = (uuid.uuid4(), uuid.uuid4(), now - timedelta(seconds=1), None)
    fake_lookup[tokens.hash_token(revoked)] = (uuid.uuid4(), uuid.uuid4(), now + timedelta(days=1), now)
    for presented in (expired, revoked, tokens.generate_raw_token(), "eyJhbGciOiJIUzI1NiJ9.e30.sig", "", "scim_", "x" * 500, good_hash + "x"):
        with pytest.raises(ScimError) as info:
            await tokens.authenticate(None, presented)  # type: ignore[arg-type]
        assert (info.value.status, info.value.detail, info.value.code) == (401, "Invalid or expired SCIM credentials.", "scim.unauthorized")
        assert info.value.headers["WWW-Authenticate"].startswith("Bearer")


@pytest.mark.asyncio
async def test_a_signed_access_token_is_never_a_scim_credential(fake_lookup, monkeypatch) -> None:
    from app.core.auth import service as auth_service
    from tests.core.auth.test_sso_sessions_provisioning import _settings

    monkeypatch.setattr(auth_service, "get_settings", _settings)
    jwt_token, _i, _e = auth_service._issue_access_token(uuid.uuid4(), uuid.uuid4())
    with pytest.raises(ScimError) as info:
        await tokens.authenticate(None, jwt_token)  # type: ignore[arg-type]
    assert info.value.status == 401
