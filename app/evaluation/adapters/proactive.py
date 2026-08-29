"""The proactive-findings authorization/lifecycle "system under test" seam.

`ProactiveAdapter` is a `typing.Protocol`, same structural-typing
convention as `RetrievalAdapter`/`MemoryAdapter`/`GraphAdapter`.

`FixtureProactiveAdapter` (Mode 1) mirrors `core.proactive.service`'s real
read-side rules against the in-memory fixture corpus: a finding-level
`incident:read` gate on its `project_id`, per-evidence-row resolution
(incidents ride the finding-level gate; documents additionally need
`published` or `knowledge:review`), deleted evidence excluded
unconditionally, and support recomputed from only what survives -- below
the finding type's own threshold hides the finding entirely. It reuses the
REAL `Identity.has_permission` rather than reimplementing permission logic,
the same choice `FixtureGraphAdapter` already makes for the identical
reason.

`RealProactiveAdapter` (Mode 2/3) wraps the actual
`core.proactive.service.list_findings`. Code-complete and not exercised
end-to-end here for the same reason as every other `Real*Adapter`: no live
database in this environment.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.proactive.contract import counts_toward_support, get_finding_spec
from app.evaluation.fixtures.proactive_corpus import (
    EVIDENCE,
    FINDINGS,
    EvidenceFixture,
    FindingFixture,
)
from app.evaluation.schemas import EvaluationCase


@runtime_checkable
class ProactiveAdapter(Protocol):
    async def list_findings(self, case: EvaluationCase) -> list[str]:
        """Return the LABELS of findings visible to `case.identity`."""
        ...


def _document_visible(evidence: EvidenceFixture, identity, project_uuid: uuid.UUID | None) -> bool:
    if evidence.deleted:
        return False
    if evidence.document_status == "published":
        return True
    return identity.has_permission("knowledge:review", project_id=project_uuid)


def _incident_visible(evidence: EvidenceFixture) -> bool:
    # Incident evidence rides entirely on the finding-level `incident:read`
    # gate already checked before evidence resolution begins -- both
    # fixture finding types are single-project-scoped, matching
    # `core.proactive.service`'s documented single-project-scope assumption.
    return not evidence.deleted


class FixtureProactiveAdapter:
    def __init__(
        self,
        findings: list[FindingFixture] | None = None,
        evidence: list[EvidenceFixture] | None = None,
    ) -> None:
        self._findings = findings if findings is not None else FINDINGS
        self._evidence = evidence if evidence is not None else EVIDENCE

    def _visible(self, finding: FindingFixture, case: EvaluationCase) -> bool:
        if finding.organization_id != case.organization_id:
            return False
        if finding.status != "active":
            return False

        identity = case.identity.to_identity(case.organization_uuid)
        project_uuid = uuid.UUID(finding.project_id) if finding.project_id else None
        if not identity.has_permission("incident:read", project_id=project_uuid):
            return False

        spec = get_finding_spec(finding.finding_type)
        support = 0
        for item in self._evidence:
            if item.finding_label != finding.label:
                continue
            if item.entity_type == "document":
                visible = _document_visible(item, identity, project_uuid)
            elif item.entity_type == "incident":
                visible = _incident_visible(item)
            else:  # unknown entity type fails closed, as in production
                visible = False
            if visible and counts_toward_support(item.role):
                support += 1

        return support >= spec.minimum_support

    async def list_findings(self, case: EvaluationCase) -> list[str]:
        return sorted(finding.label for finding in self._findings if self._visible(finding, case))


class RealProactiveAdapter:
    """Mode 2/3: the real `core.proactive.service.list_findings`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_findings(self, case: EvaluationCase) -> list[str]:
        from app.core.proactive import service as proactive_service

        actor = case.identity.to_identity(case.organization_uuid)
        findings = await proactive_service.list_findings(self._session, actor)
        # Real findings have UUIDs, not labels -- a dataset run against real
        # data would need its own id mapping. Returned as strings so the
        # runner's comparison logic is identical either way.
        return [str(finding.id) for finding in findings]
