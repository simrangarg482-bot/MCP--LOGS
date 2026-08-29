"""AI cost budget enforcement (Phase 6.6).

Owned by: agents/. Distinct from `app.agents.telemetry` (Phase 5, which only
*records* what an execution actually spent, after the fact) -- this module
*enforces* a ceiling, checked before a further LLM call is allowed to start.
Phase 5 built the telemetry this depends on; without real `prompt_tokens`/
`completion_tokens` data already being captured, there would be nothing to
check a budget against.

Deliberately organization-scoped, not global or per-user: PROJECT_PLAN.md's
own multi-tenant framing treats "one organization's usage" as the natural
cost-accountability unit (the same unit `app.ingestion.rate_limiter`'s
per-organization request budget already uses for an analogous reason) --
one heavy user in an otherwise-quiet organization should still be capped by
that organization's own ceiling, not exempted because other users in the
same org haven't spent much.

Fails *open* when unset (`Settings.max_organization_cost_usd_per_day is
None`) -- this codebase does not invent a "reasonable" default dollar cap
for a deployment it knows nothing about (see that setting's own
description). Fails *closed* (refuses the call) only once an operator has
explicitly configured a real threshold and real usage has crossed it --
matching PROJECT_PLAN.md section 12.8's "least privilege"/fail-closed value
applied to spend rather than access.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import repository
from app.agents.telemetry import get_estimated_cost_usd
from app.core.exceptions import CostBudgetExceededError
from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings

logger = get_logger(__name__)

_BUDGET_WINDOW = timedelta(hours=24)


async def check_cost_budget(session: AsyncSession, organization_id: uuid.UUID) -> None:
    """Raise `CostBudgetExceededError` if `organization_id` has already
    accumulated an estimated spend at or above `Settings.
    max_organization_cost_usd_per_day` within the trailing 24 hours.

    A no-op (returns immediately) when that setting is unset -- see this
    module's own docstring for why "unset" means "no enforcement," not "use
    some default."

    Priced against `Settings.agent_llm_model` (the *currently configured*
    model) -- the same simplification `agents.service.
    get_agent_execution_stats`'s own `estimated_cost_usd` field already
    makes, for the same reason (a single global model setting that changes
    rarely; see that function's own docstring for the full caveat).
    """
    settings = get_settings()
    if settings.max_organization_cost_usd_per_day is None:
        return

    since = datetime.now(UTC) - _BUDGET_WINDOW
    prompt_tokens, completion_tokens = await repository.get_organization_token_usage_since(
        session, organization_id, since
    )
    estimated_cost = get_estimated_cost_usd(settings.agent_llm_model, prompt_tokens, completion_tokens)

    if estimated_cost is None:
        # Genuinely no priced usage to compare (e.g. `agent_llm_model` isn't
        # in the pricing table at all, or nothing has been spent yet) --
        # never treated as "budget exceeded"; see `get_estimated_cost_usd`'s
        # own docstring for why `None` means "unknown," not "free."
        return

    if estimated_cost >= settings.max_organization_cost_usd_per_day:
        logger.warning(
            "organization_cost_budget_exceeded",
            organization_id=str(organization_id),
            estimated_cost_usd=estimated_cost,
            budget_usd=settings.max_organization_cost_usd_per_day,
        )
        raise CostBudgetExceededError(
            "This organization has exceeded its daily AI usage budget. "
            "Please try again later or contact your administrator.",
            error_code="cost_budget_exceeded",
            detail={
                "estimated_cost_usd": estimated_cost,
                "budget_usd": settings.max_organization_cost_usd_per_day,
            },
        )
