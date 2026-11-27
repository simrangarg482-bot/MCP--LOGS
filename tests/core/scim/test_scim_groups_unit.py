"""Group payload / PATCH / filter parsing and resource shaping (pure, no database)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core.scim import schemas
from app.core.scim.errors import ScimError

PATCH = schemas.PATCH_SCHEMA
A, B = str(uuid.uuid4()), str(uuid.uuid4())


def _patch(*operations):
    return schemas.parse_group_patch({"schemas": [PATCH], "Operations": list(operations)})


def test_entra_member_operations() -> None:
    f = _patch({"op": "Add", "path": "members", "value": [{"value": A}]},
               {"op": "Remove", "path": f'members[value eq "{B}"]'},
               {"op": "Replace", "path": "displayName", "value": "Ops"})
    assert f.member_ops == [("add", [A]), ("remove", [B])]
    assert f.display_name == "Ops" and f.provided == {"members", "displayName"}


def test_okta_member_operations_and_pathless_values() -> None:
    f = _patch({"op": "remove", "path": "members", "value": [{"value": A}, {"value": A}]},
               {"op": "replace", "value": {"displayName": "X", "externalId": "e"}},
               {"op": "add", "value": {"members": [{"value": B}]}})
    assert f.member_ops == [("remove", [A]), ("add", [B])]  # duplicates collapse
    assert (f.display_name, f.external_id) == ("X", "e")


def test_remove_without_a_value_removes_everyone_and_unknown_attributes_are_ignored() -> None:
    f = _patch({"op": "remove", "path": "members"}, {"op": "add", "path": "description", "value": "ignored"},
               {"op": "add", "path": "urn:x:ext:attr", "value": 1})
    assert f.member_ops == [("remove", None)]


@pytest.mark.parametrize("body", [
    None, [], {}, {"Operations": []}, {"Operations": "x"}, {"Operations": [1]},
    {"Operations": [{"op": "move", "path": "members"}]},
    {"Operations": [{"op": "remove", "path": "displayName"}]},
    {"Operations": [{"op": "add", "path": "displayName", "value": ""}]},
    {"Operations": [{"op": "add", "path": "members", "value": [{"display": "x"}]}]},
    {"Operations": [{"op": "add", "path": "members", "value": "x"}]},
    {"Operations": [{"op": "add", "path": 'members[value eq "a"]', "value": []}]},
    {"Operations": [{"op": "add", "value": "not an object"}]},
    {"Operations": [{"op": "add", "path": 5}]},
    {"Operations": [{"op": "add", "path": "members", "value": [{"value": str(i)} for i in range(1001)]}]},
    {"Operations": [{"op": "add", "path": "members", "value": [{"value": str(i)} for i in range(600)]}, {"op": "add", "path": "members", "value": [{"value": str(i + 1000)} for i in range(600)]}]},
])
def test_malformed_patches_are_scim_400s(body) -> None:
    with pytest.raises(ScimError) as err:
        schemas.parse_group_patch(body)
    assert err.value.status == 400


def test_group_payload_requires_a_name_and_validates_members() -> None:
    assert schemas.parse_group_payload({"displayName": "G", "members": [{"value": A}]}).member_ops == [("replace", [A])]
    assert schemas.parse_group_payload({"displayName": "G"}).member_ops == []  # absent members: a PUT leaves them alone
    assert schemas.parse_group_payload({"displayName": "G", "members": []}).member_ops == [("replace", [])]
    for bad in (None, {}, {"displayName": " "}, {"displayName": "G", "schemas": ["x"]}, {"displayName": "G", "members": [{}]}):
        with pytest.raises(ScimError):
            schemas.parse_group_payload(bad)


def test_control_characters_in_names_are_refused_not_stored() -> None:
    for bad in ("a" + chr(0) + "b", "a" + chr(10) + "b", "x" + chr(31) + "y"):
        with pytest.raises(ScimError):
            schemas.parse_group_payload({"displayName": bad})
        with pytest.raises(ScimError):
            schemas.parse_user_payload({"userName": bad}, require_user_name=True)
        with pytest.raises(ScimError):
            schemas.parse_patch({"Operations": [{"op": "replace", "path": "displayName", "value": bad}]})


def test_group_filters_are_limited_to_displayname_and_externalid() -> None:
    assert schemas.parse_group_filter('displayName eq "A \\"b\\""') == ("displayName", 'A "b"')
    assert schemas.parse_group_filter('EXTERNALID eq "x"') == ("externalId", "x")
    assert schemas.parse_group_filter(None) is None and schemas.parse_group_filter("  ") is None
    for bad in ('displayName co "a"', 'members.value eq "x"', 'displayName eq "a" and externalId eq "b"', "displayName"):
        with pytest.raises(ScimError) as err:
            schemas.parse_group_filter(bad)
        assert err.value.scim_type == "invalidFilter"


def test_group_resource_shape() -> None:
    now = datetime.now(timezone.utc)
    g = SimpleNamespace(id=uuid.uuid4(), display_name="G", external_id=None, created_at=now, updated_at=now)
    res = schemas.group_resource(g, [A], "https://x/scim/v2")
    assert res["schemas"] == [schemas.GROUP_SCHEMA] and res["members"] == [
        {"value": A, "$ref": f"https://x/scim/v2/Users/{A}", "type": "User"}
    ]
    assert "externalId" not in res and res["meta"]["location"].endswith(f"/Groups/{g.id}")
