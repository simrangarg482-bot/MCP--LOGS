"""Invariants of the assignable-permission catalog and the starter roles.

No database: these pin the code-level contract that the migration
`e8c3a1f5b7d2` and the role-management API both rely on.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.core.users import permissions as catalog
from app.core.users.repository import ADMIN_PERMISSION_CODES
from app.core.users.role_schemas import RoleCreate, RoleUpdate

_MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "app/database/migrations/versions/e8c3a1f5b7d2_org_scoped_custom_roles.py"
)


def _migration():
    spec = importlib.util.spec_from_file_location("roles_migration", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_catalog_has_no_duplicates_and_complete_metadata() -> None:
    codes = [p.code for p in catalog.ASSIGNABLE_PERMISSIONS]
    assert len(codes) == len(set(codes))
    assert all(p.label and p.description and p.group for p in catalog.ASSIGNABLE_PERMISSIONS)


def test_internal_permissions_are_never_assignable() -> None:
    assert "system:graph_discovery" not in catalog.ASSIGNABLE_PERMISSION_CODES
    assert not any(code.startswith("system:") for code in catalog.ASSIGNABLE_PERMISSION_CODES)


def test_admin_role_carries_exactly_the_catalog() -> None:
    assert set(ADMIN_PERMISSION_CODES) == catalog.ASSIGNABLE_PERMISSION_CODES
    assert catalog.ADMIN_PERMISSION in catalog.ASSIGNABLE_PERMISSION_CODES


def test_catalog_and_migration_have_not_drifted_apart() -> None:
    """Built-in roles are immutable at runtime, so a new permission needs a
    migration: this fails the moment code and migration disagree."""
    migration = _migration()
    assert {code for code, _ in migration._PERMISSIONS} == catalog.ASSIGNABLE_PERMISSION_CODES
    code_templates = {
        t.name: (t.description, set(t.permissions)) for t in catalog.DEFAULT_ROLE_TEMPLATES
    }
    migration_templates = {n: (d, set(c)) for n, d, c in migration._STARTER_ROLES}
    assert migration_templates == code_templates
    assert migration._ADMIN_NAME == catalog.BUILTIN_ADMIN_ROLE_NAME


def test_starter_roles_use_only_assignable_codes_and_none_is_an_administrator() -> None:
    names = [t.name for t in catalog.DEFAULT_ROLE_TEMPLATES]
    assert names == ["viewer", "engineer", "reviewer"]
    for template in catalog.DEFAULT_ROLE_TEMPLATES:
        assert set(template.permissions) <= catalog.ASSIGNABLE_PERMISSION_CODES
        assert catalog.ADMIN_PERMISSION not in template.permissions
        assert len(template.permissions) == len(set(template.permissions))


@pytest.mark.parametrize(
    "name,expected",
    [("  Incident   Commander ", "Incident Commander"), ("a.b-c_d 1", "a.b-c_d 1")],
)
def test_role_names_are_normalized(name, expected) -> None:
    assert RoleCreate(name=name).name == expected
    assert RoleUpdate(name=name).name == expected


@pytest.mark.parametrize(
    "name",
    ["", "   ", "x", "a" * 65, "-lead", "_lead", "bad/name", "<b>x</b>", "semi;colon", "lìne"],
)
def test_invalid_role_names_are_rejected(name) -> None:
    with pytest.raises(ValueError):
        RoleCreate(name=name)


def test_description_is_trimmed_bounded_and_blank_means_none() -> None:
    assert RoleCreate(name="okay", description="  hi  ").description == "hi"
    assert RoleCreate(name="okay", description="   ").description is None
    with pytest.raises(ValueError):
        RoleCreate(name="okay", description="x" * 301)


def test_update_distinguishes_unset_from_cleared_description() -> None:
    assert "description" not in RoleUpdate(name="okay").model_fields_set
    assert "description" in RoleUpdate(description=None).model_fields_set
