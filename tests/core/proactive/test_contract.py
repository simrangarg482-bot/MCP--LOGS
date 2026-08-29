"""Tests for `app.core.proactive.contract` -- the finding-type vocabulary.

Pure functions, no database, no mocking.
"""

from __future__ import annotations

import pytest

from app.core.proactive import contract


def test_valid_finding_type_resolves():
    spec = contract.get_finding_spec("recurring_incident_severity")
    assert spec.scope == "project"
    assert spec.minimum_support >= 1


def test_unknown_finding_type_is_rejected():
    with pytest.raises(contract.InvalidFindingTypeError, match="unknown finding type"):
        contract.get_finding_spec("service_degradation_trend")


def test_only_two_finding_types_are_supported():
    """Locks in the deliberately small vocabulary -- see the module
    docstring for why nothing else qualified from repository discovery."""
    assert {"recurring_incident_severity", "incident_multi_document"} == contract.FINDING_TYPES


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("supporting_incident", True),
        ("supporting_document", True),
        ("anchor_incident", False),
        ("primary_incident", False),
    ],
)
def test_counts_toward_support_follows_the_role_prefix_convention(role, expected):
    assert contract.counts_toward_support(role) is expected


def test_recurring_incident_severity_only_declares_the_supporting_role():
    spec = contract.get_finding_spec("recurring_incident_severity")
    assert spec.evidence_roles == ("supporting_incident",)


def test_incident_multi_document_declares_anchor_and_supporting_roles():
    spec = contract.get_finding_spec("incident_multi_document")
    assert set(spec.evidence_roles) == {"anchor_incident", "supporting_document"}
