"""Tests for `app.core.graph.contract` -- the relationship vocabulary.

Pure functions, no database, no mocking: every legal/illegal combination is
either in the tuples or it isn't.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.graph import contract


def test_valid_derived_triple_resolves():
    spec = contract.get_spec("document", "documents", "incident")
    assert spec.provenance_type == "deterministic_extraction"
    assert spec.symmetric is False


def test_valid_foreign_key_triple_resolves():
    spec = contract.get_spec("incident", "has_postmortem", "postmortem")
    assert spec.provenance_type == "foreign_key"


def test_unknown_source_type_is_rejected():
    with pytest.raises(contract.InvalidRelationshipError, match="unknown source"):
        contract.get_spec("service", "belongs_to", "project")


def test_unknown_target_type_is_rejected():
    with pytest.raises(contract.InvalidRelationshipError, match="unknown target"):
        contract.get_spec("incident", "belongs_to", "service")


def test_unknown_relationship_type_is_rejected():
    with pytest.raises(contract.InvalidRelationshipError, match="unknown relationship"):
        contract.get_spec("incident", "caused_by", "incident")


def test_known_types_in_an_unsupported_combination_are_rejected():
    """Every component is individually valid, but this triple isn't in the
    contract -- the case a naive "is it in the enum?" check would wave
    through."""
    with pytest.raises(contract.InvalidRelationshipError, match="not a valid relationship"):
        contract.get_spec("postmortem", "documents", "incident")


def test_get_derived_spec_rejects_a_foreign_key_backed_triple():
    """A write path must never be able to store a copy of something Postgres
    already enforces via FK."""
    with pytest.raises(contract.InvalidRelationshipError, match="foreign key"):
        contract.get_derived_spec("incident", "has_postmortem", "postmortem")


def test_get_derived_spec_accepts_a_genuinely_derived_triple():
    spec = contract.get_derived_spec("incident", "related_to", "incident")
    assert spec.symmetric is True


def test_canonical_direction_orders_symmetric_relationships_by_lowest_uuid_first():
    spec = contract.get_derived_spec("incident", "related_to", "incident")
    a, b = uuid.uuid4(), uuid.uuid4()
    low, high = (a, b) if str(a) <= str(b) else (b, a)

    forward = contract.canonical_direction(spec, a, b)
    backward = contract.canonical_direction(spec, b, a)

    assert forward == (low, high)
    assert backward == (low, high), "canonical order must not depend on call order"


def test_canonical_direction_leaves_directional_relationships_unchanged():
    spec = contract.get_spec("document", "documents", "incident")
    source_id, target_id = uuid.uuid4(), uuid.uuid4()
    assert contract.canonical_direction(spec, source_id, target_id) == (source_id, target_id)


def test_entity_and_relationship_type_vocabularies_have_no_invented_entity():
    """Notably absent per the module docstring: service/system/application/
    component. Locking this in as a test means a future PR that quietly adds
    one of those must change this test deliberately, not by accident."""
    assert "service" not in contract.ENTITY_TYPES
    assert "system" not in contract.ENTITY_TYPES
    assert "component" not in contract.ENTITY_TYPES
