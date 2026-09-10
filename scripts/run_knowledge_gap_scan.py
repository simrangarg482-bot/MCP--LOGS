"""Manually run the Knowledge Gap Agent for one organization, on demand --
bypassing `scheduled_knowledge_gap_scan`'s 2am cron (app.agents.workers.main),
which is otherwise the ONLY thing that ever triggers
`agents.service.detect_knowledge_gaps` (see that function's own docstring:
"there is no REST/MCP action that triggers a fresh run on demand, only
reads the results"). Useful for testing the low-confidence-question ->
GapReport flow without waiting for the schedule.

This calls the exact same `agents.service.detect_knowledge_gaps` the cron
job calls, with `Identity.for_agent("knowledge_gap_agent", organization_id)`
-- the identical system-triggered identity `run_knowledge_gap_detection_task`
constructs -- so its behavior (clustering low-confidence `answer_question`
executions, merging into existing open reports, recording an
`agent_executions` row) is identical to a real scheduled run for this one
organization. It does NOT lower/bypass any of the pipeline's own thresholds
(`confidence_threshold`, `knowledge_gap_min_cluster_size`,
`knowledge_gap_similarity_threshold`, `knowledge_gap_lookback_days`) -- if
nothing qualifies, this prints zero reports, the same as a real scheduled
run would.

Run:
    python scripts/run_knowledge_gap_scan.py                   # lists organizations, prompts for one
    python scripts/run_knowledge_gap_scan.py <organization_id>  # runs directly
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from app.agents import service as agents_service
from app.core.tenancy import service as tenancy_service
from app.database.session import session_scope, set_tenant_context
from app.shared.config.logging import configure_logging
from app.shared.schemas import Identity

configure_logging()


async def _resolve_organization_id(session, raw_arg: str | None) -> uuid.UUID:
    if raw_arg:
        return uuid.UUID(raw_arg)

    organizations = await tenancy_service.list_organizations(session)
    if not organizations:
        print("No organizations exist yet.")
        sys.exit(1)
    if len(organizations) == 1:
        return organizations[0].id

    print("Multiple organizations found -- pass one explicitly:")
    for org in organizations:
        print(f"  {org.id}  {org.name}")
    sys.exit(1)


async def main() -> None:
    raw_arg = sys.argv[1] if len(sys.argv) > 1 else None

    async with session_scope() as session:
        organization_id = await _resolve_organization_id(session, raw_arg)
        await set_tenant_context(session, organization_id)

        actor = Identity.for_agent("knowledge_gap_agent", organization_id)
        reports = await agents_service.detect_knowledge_gaps(session, actor)

    print(f"\nKnowledge Gap Agent run complete for organization {organization_id}.")
    print(f"{len(reports)} open gap report(s) now exist for this organization:\n")
    for report in reports:
        print(f"  - {report.suggested_topic}")
        print(f"    status={report.status}  action={report.suggested_action}  "
              f"supporting_queries={len(report.supporting_execution_ids)}")

    if not reports:
        print(
            "  (none -- either no low-confidence `answer_question` executions "
            "exist within the lookback window, or fewer than "
            "knowledge_gap_min_cluster_size similar ones were found; see "
            "settings.knowledge_gap_* for the exact thresholds in play)"
        )


if __name__ == "__main__":
    asyncio.run(main())
