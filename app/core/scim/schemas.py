"""SCIM 2.0 resource shapes, filter parsing and payload normalisation (RFC 7643 / 7644).

Everything here is pure (no database). The identity providers differ in small ways and this module is where
those differences are absorbed:

* Microsoft Entra ID sends `op` as `Replace` / `Add` / `Remove` (capitalised), `active` as the STRING
  `"False"` / `"True"`, patches with and without a `path`, many attributes EKIP does not store (title,
  department, `urn:...:enterprise:2.0:User:manager`), and looks users up with `userName eq "..."` or
  `externalId eq "..."`.
* Okta sends `active` as a real boolean, uses PUT with the whole resource as well as PATCH, and probes with
  `startIndex=1&count=1`.

Unknown attributes are IGNORED (a provisioning cycle must not fail because the IdP syncs a job title);
attributes that would change EKIP's access model (`password`, `groups` on a user) are refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.core.scim.errors import ScimError

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SERVICE_PROVIDER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
SCHEMA_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Schema"
RESOURCE_TYPE_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ResourceType"

MAX_PAGE = 100
MAX_PATCH_OPERATIONS = 100
_MAX_USER_NAME = 320
_MAX_EXTERNAL_ID = 256
_MAX_DISPLAY_NAME = 256

# Attributes EKIP stores for a user.
_STORED = {"active", "username", "externalid", "displayname"}
# Attributes that are accepted and ignored (the IdP may sync them; EKIP has no use for them).
_IGNORED = {
    "name", "emails", "title", "usertype", "nickname", "profileurl", "locale", "timezone", "preferredlanguage",
    "phonenumbers", "addresses", "photos", "ims", "roles", "entitlements", "x509certificates", "meta", "id",
}
# Refused: they would change EKIP's access model, and SCIM Users here never carry them.
_REFUSED = {"password": "mutability", "groups": "mutability"}


def _bad(detail: str, scim_type: str, code: str) -> ScimError:
    return ScimError(400, detail, scim_type, code=code)


# ---------------------------------------------------------------------------------------------- values


def parse_bool(value: Any) -> bool:
    """`true`/`false` as a JSON boolean, or as the strings Entra sends (`"True"`, `"False"`)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise _bad("'active' must be true or false.", "invalidValue", "scim.invalid_value")


def _text(value: Any, attr: str, limit: int, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise _bad(f"'{attr}' is required.", "invalidValue", "scim.invalid_value")
        return None
    if not isinstance(value, str):
        raise _bad(f"'{attr}' must be a string.", "invalidValue", "scim.invalid_value")
    if any(ord(ch) < 32 and ch != chr(9) for ch in value):
        # NUL and other control characters: PostgreSQL cannot store NUL, and none of these belongs in a name.
        raise _bad(f"'{attr}' contains invalid characters.", "invalidValue", "scim.invalid_value")
    stripped = value.strip()
    if not stripped:
        if required:
            raise _bad(f"'{attr}' is required.", "invalidValue", "scim.invalid_value")
        return None
    if len(stripped) > limit:
        raise _bad(f"'{attr}' is too long.", "invalidValue", "scim.invalid_value")
    return stripped


def user_name_key(user_name: str) -> str:
    return user_name.strip().lower()


# ---------------------------------------------------------------------------------------------- payloads


@dataclass
class UserFields:
    """The EKIP-relevant fields of a User payload. `None` means "not provided"."""

    user_name: str | None = None
    external_id: str | None = None
    display_name: str | None = None
    active: bool | None = None
    provided: set[str] = field(default_factory=set)


def _display_name_from(payload: dict) -> str | None:
    if "displayName" in payload:
        return _text(payload.get("displayName"), "displayName", _MAX_DISPLAY_NAME)
    name = payload.get("name")
    if isinstance(name, dict):
        formatted = name.get("formatted")
        if isinstance(formatted, str) and formatted.strip():
            return _text(formatted, "name.formatted", _MAX_DISPLAY_NAME)
        parts = [name.get("givenName"), name.get("familyName")]
        joined = " ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
        return joined[:_MAX_DISPLAY_NAME] or None
    return None


def parse_user_payload(payload: Any, *, require_user_name: bool) -> UserFields:
    """A POST or PUT body."""
    if not isinstance(payload, dict):
        raise _bad("The request body must be a JSON object.", "invalidSyntax", "scim.invalid_syntax")
    schemas = payload.get("schemas")
    if schemas is not None and (not isinstance(schemas, list) or USER_SCHEMA not in schemas):
        raise _bad("The body must declare the SCIM core User schema.", "invalidSyntax", "scim.invalid_syntax")
    if "password" in payload:
        raise ScimError(400, "Passwords are not managed through SCIM.", "mutability", code="scim.mutability")
    fields = UserFields()
    if "userName" in payload or require_user_name:
        fields.user_name = _text(payload.get("userName"), "userName", _MAX_USER_NAME, required=require_user_name)
        if fields.user_name is not None:
            fields.provided.add("userName")
    if "externalId" in payload:
        fields.external_id = _text(payload.get("externalId"), "externalId", _MAX_EXTERNAL_ID)
        fields.provided.add("externalId")
    name = _display_name_from(payload)
    if "displayName" in payload or isinstance(payload.get("name"), dict):
        fields.display_name = name
        fields.provided.add("displayName")
    if "active" in payload:
        fields.active = parse_bool(payload["active"])
        fields.provided.add("active")
    return fields


def parse_patch(body: Any) -> UserFields:
    """A PATCH body (RFC 7644 section 3.5.2) reduced to the fields EKIP stores. The LAST operation touching
    a field wins, as the RFC specifies. Operation names are case-insensitive."""
    if not isinstance(body, dict):
        raise _bad("The request body must be a JSON object.", "invalidSyntax", "scim.invalid_syntax")
    operations = body.get("Operations", body.get("operations"))
    if not isinstance(operations, list) or not operations:
        raise _bad("A PATCH request needs a non-empty 'Operations' list.", "invalidSyntax", "scim.invalid_syntax")
    if len(operations) > MAX_PATCH_OPERATIONS:
        raise _bad("Too many operations in one request.", "tooMany", "scim.too_many_operations")

    fields = UserFields()

    def apply(attr_path: str, value: Any, op: str) -> None:
        base = re.split(r"[.\[:]", attr_path, maxsplit=1)[0] if not attr_path.lower().startswith("urn:") else ""
        if attr_path.lower().startswith("urn:"):
            return  # enterprise / custom extension attributes: not stored
        key = base.strip().lower()
        if key in _REFUSED:
            raise ScimError(400, f"'{key}' cannot be modified through SCIM.", _REFUSED[key], code="scim.mutability")
        if key in _IGNORED or not key:
            return
        if key not in _STORED:
            return  # an attribute EKIP does not know: ignore rather than fail the provisioning cycle
        if "." in attr_path or "[" in attr_path:
            return
        if op == "remove":
            if key in ("active", "username"):
                raise ScimError(400, f"'{key}' cannot be removed.", "mutability", code="scim.mutability")
            if key == "externalid":
                fields.external_id, _ = None, fields.provided.add("externalId")
            elif key == "displayname":
                fields.display_name, _ = None, fields.provided.add("displayName")
            return
        if key == "active":
            fields.active = parse_bool(value)
            fields.provided.add("active")
        elif key == "username":
            fields.user_name = _text(value, "userName", _MAX_USER_NAME, required=True)
            fields.provided.add("userName")
        elif key == "externalid":
            fields.external_id = _text(value, "externalId", _MAX_EXTERNAL_ID)
            fields.provided.add("externalId")
        elif key == "displayname":
            fields.display_name = _text(value, "displayName", _MAX_DISPLAY_NAME)
            fields.provided.add("displayName")

    for operation in operations:
        if not isinstance(operation, dict):
            raise _bad("Each operation must be an object.", "invalidSyntax", "scim.invalid_syntax")
        op = str(operation.get("op", "")).strip().lower()
        if op not in ("add", "replace", "remove"):
            raise _bad("Unsupported operation.", "invalidSyntax", "scim.invalid_syntax")
        path = operation.get("path")
        value = operation.get("value")
        if path is None or (isinstance(path, str) and not path.strip()):
            # No path: the value is an object of attributes (Entra and Okta both do this)
            if not isinstance(value, dict):
                raise _bad("An operation without a path needs an object value.", "invalidValue", "scim.invalid_value")
            for attr, attr_value in value.items():
                apply(str(attr), attr_value, op)
        else:
            if not isinstance(path, str):
                raise _bad("'path' must be a string.", "invalidPath", "scim.invalid_path")
            apply(path.strip(), value, op)
    return fields


# ---------------------------------------------------------------------------------------------- filter

_FILTER = re.compile(r'^\s*(userName|externalId)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$', re.IGNORECASE)


def parse_filter(filter_text: str | None) -> tuple[str, str] | None:
    """Only `userName eq "x"` and `externalId eq "x"` -- the lookups Entra and Okta actually use. Anything
    else is refused with `invalidFilter` rather than silently returning everything."""
    if filter_text is None or not filter_text.strip():
        return None
    match = _FILTER.match(filter_text)
    if not match:
        raise _bad(
            "Only 'userName eq \"...\"' and 'externalId eq \"...\"' filters are supported.",
            "invalidFilter",
            "scim.invalid_filter",
        )
    attr = "userName" if match.group(1).lower() == "username" else "externalId"
    value = match.group(2).replace('\\"', '"').replace("\\\\", "\\")
    if any(ord(ch) < 32 for ch in value):
        raise _bad("The filter value contains invalid characters.", "invalidFilter", "scim.invalid_filter")
    return attr, value


# ---------------------------------------------------------------------------------------------- resources


def user_resource(identity: Any, base_url: str, *, effective_active: bool) -> dict:
    location = f"{base_url.rstrip('/')}/Users/{identity.id}"
    resource: dict = {
        "schemas": [USER_SCHEMA],
        "id": str(identity.id),
        "userName": identity.user_name,
        "active": effective_active,
        "meta": {
            "resourceType": "User",
            "created": identity.created_at.isoformat(),
            "lastModified": identity.updated_at.isoformat(),
            "location": location,
        },
    }
    if identity.external_id:
        resource["externalId"] = identity.external_id
    if identity.display_name:
        resource["displayName"] = identity.display_name
    return resource


def list_response(resources: list[dict], total: int, start_index: int) -> dict:
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


def clamp_paging(start_index: int | None, count: int | None) -> tuple[int, int]:
    start = 1 if start_index is None or start_index < 1 else start_index
    size = MAX_PAGE if count is None else max(0, min(count, MAX_PAGE))
    return start, size


# ---------------------------------------------------------------------------------------------- discovery


def service_provider_config(base_url: str) -> dict:
    return {
        "schemas": [SERVICE_PROVIDER_SCHEMA],
        "documentationUri": f"{base_url.rstrip('/')}/docs",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": MAX_PAGE},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "Bearer token",
                "description": "An organization-specific SCIM token created in Settings -> Provisioning.",
                "primary": True,
            }
        ],
        "meta": {"resourceType": "ServiceProviderConfig", "location": f"{base_url.rstrip('/')}/ServiceProviderConfig"},
    }


def _attr(name: str, type_: str = "string", *, required: bool = False, mutability: str = "readWrite",
          unique: str = "none", description: str = "") -> dict:
    return {
        "name": name, "type": type_, "multiValued": False, "required": required, "caseExact": False,
        "mutability": mutability, "returned": "default", "uniqueness": unique, "description": description,
    }


def user_schema(base_url: str) -> dict:
    return {
        "schemas": [SCHEMA_SCHEMA],
        "id": USER_SCHEMA,
        "name": "User",
        "description": "An identity the identity provider manages for this organization.",
        "attributes": [
            _attr("userName", required=True, unique="server", description="Unique login name (usually the email)."),
            _attr("externalId", description="The identity provider's immutable id for the user."),
            _attr("displayName"),
            _attr("active", "boolean", description="false deactivates the member's access to THIS organization."),
        ],
        "meta": {"resourceType": "Schema", "location": f"{base_url.rstrip('/')}/Schemas/{USER_SCHEMA}"},
    }


def group_schema(base_url: str) -> dict:
    return {
        "schemas": [SCHEMA_SCHEMA],
        "id": GROUP_SCHEMA,
        "name": "Group",
        "description": "A group of identities; mapped to EKIP roles by an administrator.",
        "attributes": [
            _attr("displayName", required=True),
            _attr("externalId"),
            {**_attr("members", "complex"), "multiValued": True},
        ],
        "meta": {"resourceType": "Schema", "location": f"{base_url.rstrip('/')}/Schemas/{GROUP_SCHEMA}"},
    }


def resource_types(base_url: str, *, groups: bool) -> list[dict]:
    base = base_url.rstrip("/")
    types = [
        {
            "schemas": [RESOURCE_TYPE_SCHEMA], "id": "User", "name": "User", "endpoint": "/Users",
            "schema": USER_SCHEMA,
            "meta": {"resourceType": "ResourceType", "location": f"{base}/ResourceTypes/User"},
        }
    ]
    if groups:
        types.append(
            {
                "schemas": [RESOURCE_TYPE_SCHEMA], "id": "Group", "name": "Group", "endpoint": "/Groups",
                "schema": GROUP_SCHEMA,
                "meta": {"resourceType": "ResourceType", "location": f"{base}/ResourceTypes/Group"},
            }
        )
    return types


# ================================================================================================ Groups

MAX_GROUP_MEMBER_VALUES = 1000  # member references in ONE request (all operations together)
_GROUP_FILTER = re.compile(r'^\s*(displayName|externalId)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$', re.IGNORECASE)
_MEMBER_PATH = re.compile(r'^members\s*\[\s*value\s+eq\s+"([^"]*)"\s*\]$', re.IGNORECASE)


@dataclass
class GroupFields:
    """The EKIP-relevant parts of a Group payload or PATCH. `member_ops` is applied in order:
    `("replace", ids)`, `("add", ids)`, `("remove", ids)` or `("remove", None)` (remove everyone)."""

    display_name: str | None = None
    external_id: str | None = None
    provided: set[str] = field(default_factory=set)
    member_ops: list[tuple[str, list[str] | None]] = field(default_factory=list)


def _member_values(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    if len(items) > MAX_GROUP_MEMBER_VALUES:
        raise _bad("Too many members in one request.", "tooMany", "scim.too_many_members")
    out: list[str] = []
    for item in items:
        raw = item.get("value") if isinstance(item, dict) else None
        if not isinstance(raw, str) or not raw.strip():
            raise _bad("Each member needs a 'value' (the user's id).", "invalidValue", "scim.invalid_value")
        out.append(raw.strip())
    return list(dict.fromkeys(out))


def _check_total(fields: "GroupFields") -> "GroupFields":
    total = sum(len(values) for _op, values in fields.member_ops if values)
    if total > MAX_GROUP_MEMBER_VALUES:
        raise _bad("Too many members in one request.", "tooMany", "scim.too_many_members")
    return fields


def parse_group_payload(payload: Any) -> GroupFields:
    """A POST or PUT body. A PUT replaces the members only when `members` is present."""
    if not isinstance(payload, dict):
        raise _bad("The request body must be a JSON object.", "invalidSyntax", "scim.invalid_syntax")
    declared = payload.get("schemas")
    if declared is not None and (not isinstance(declared, list) or GROUP_SCHEMA not in declared):
        raise _bad("The body must declare the SCIM core Group schema.", "invalidSyntax", "scim.invalid_syntax")
    fields = GroupFields()
    fields.display_name = _text(payload.get("displayName"), "displayName", _MAX_DISPLAY_NAME, required=True)
    fields.provided.add("displayName")
    if "externalId" in payload:
        fields.external_id = _text(payload.get("externalId"), "externalId", _MAX_EXTERNAL_ID)
        fields.provided.add("externalId")
    if "members" in payload:
        fields.member_ops.append(("replace", _member_values(payload.get("members"))))
        fields.provided.add("members")
    return _check_total(fields)


def parse_group_patch(body: Any) -> GroupFields:
    """A Group PATCH: add / remove / replace of `members`, `displayName` and `externalId`. Entra removes with
    `members[value eq "id"]`; Okta sends `value: [{value: id}]`. Operation names are case-insensitive."""
    if not isinstance(body, dict):
        raise _bad("The request body must be a JSON object.", "invalidSyntax", "scim.invalid_syntax")
    operations = body.get("Operations", body.get("operations"))
    if not isinstance(operations, list) or not operations:
        raise _bad("A PATCH request needs a non-empty 'Operations' list.", "invalidSyntax", "scim.invalid_syntax")
    if len(operations) > MAX_PATCH_OPERATIONS:
        raise _bad("Too many operations in one request.", "tooMany", "scim.too_many_operations")
    fields = GroupFields()

    def set_attr(name: str, value: Any, op: str) -> None:
        key = name.strip().lower()
        if key == "displayname":
            if op == "remove":
                raise ScimError(400, "'displayName' cannot be removed.", "mutability", code="scim.mutability")
            fields.display_name = _text(value, "displayName", _MAX_DISPLAY_NAME, required=True)
            fields.provided.add("displayName")
        elif key == "externalid":
            fields.external_id = None if op == "remove" else _text(value, "externalId", _MAX_EXTERNAL_ID)
            fields.provided.add("externalId")
        elif key == "members":
            fields.provided.add("members")
            if op == "remove":
                fields.member_ops.append(("remove", _member_values(value) if value is not None else None))
            else:
                fields.member_ops.append((op, _member_values(value)))
        # any other attribute: ignored rather than failing the provisioning cycle

    for operation in operations:
        if not isinstance(operation, dict):
            raise _bad("Each operation must be an object.", "invalidSyntax", "scim.invalid_syntax")
        op = str(operation.get("op", "")).strip().lower()
        if op not in ("add", "replace", "remove"):
            raise _bad("Unsupported operation.", "invalidSyntax", "scim.invalid_syntax")
        path, value = operation.get("path"), operation.get("value")
        if path is None or (isinstance(path, str) and not path.strip()):
            if not isinstance(value, dict):
                raise _bad("An operation without a path needs an object value.", "invalidValue", "scim.invalid_value")
            for attr, attr_value in value.items():
                set_attr(str(attr), attr_value, op)
            continue
        if not isinstance(path, str):
            raise _bad("'path' must be a string.", "invalidPath", "scim.invalid_path")
        path = path.strip()
        selector = _MEMBER_PATH.match(path)
        if selector:
            if op != "remove":
                raise _bad("A member filter path is only valid for 'remove'.", "invalidPath", "scim.invalid_path")
            fields.provided.add("members")
            fields.member_ops.append(("remove", [selector.group(1).strip()]))
        elif path.lower().startswith("urn:") or "." in path or "[" in path:
            continue
        else:
            set_attr(path, value, op)
    return _check_total(fields)


def parse_group_filter(filter_text: str | None) -> tuple[str, str] | None:
    if filter_text is None or not filter_text.strip():
        return None
    match = _GROUP_FILTER.match(filter_text)
    if not match:
        raise _bad(
            "Only 'displayName eq \"...\"' and 'externalId eq \"...\"' filters are supported.",
            "invalidFilter",
            "scim.invalid_filter",
        )
    attr = "displayName" if match.group(1).lower() == "displayname" else "externalId"
    value = match.group(2).replace('\\"', '"').replace("\\\\", "\\")
    if any(ord(ch) < 32 for ch in value):
        raise _bad("The filter value contains invalid characters.", "invalidFilter", "scim.invalid_filter")
    return attr, value


def group_resource(group: Any, member_ids: list[Any], base_url: str) -> dict:
    base = base_url.rstrip("/")
    resource: dict = {
        "schemas": [GROUP_SCHEMA],
        "id": str(group.id),
        "displayName": group.display_name,
        "members": [{"value": str(m), "$ref": f"{base}/Users/{m}", "type": "User"} for m in member_ids],
        "meta": {
            "resourceType": "Group",
            "created": group.created_at.isoformat(),
            "lastModified": group.updated_at.isoformat(),
            "location": f"{base}/Groups/{group.id}",
        },
    }
    if group.external_id:
        resource["externalId"] = group.external_id
    return resource
