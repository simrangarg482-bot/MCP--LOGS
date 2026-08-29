"""Pydantic contracts for core/privacy -- the deletion plan, the per-step
outcome, and the overall result.

Owned by: core/privacy. Same "one schemas.py per module" convention as every
other `core/*` submodule.

The plan/result split is the load-bearing design here. A deletion plan is
computed by *reading* only, and can be returned to a caller (dry run) without
mutating anything; a result records what execution actually did, per step, so
a partial failure is representable rather than collapsing into a bare
success/failure boolean.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: What happens to one category of data. Mirrors the classification in
#: `docs/DATA_LIFECYCLE.md`, which derives each value from the table's real
#: foreign-key constraints rather than from preference.
DataAction = Literal["hard_delete", "anonymize", "retain"]

#: Per-step and overall execution status. `partially_completed` exists
#: because deletion is inherently multi-step: reporting a run that cleaned
#: 6 of 8 categories as either "completed" or "failed" would both be lies.
DeletionStatus = Literal["completed", "partially_completed", "failed"]

#: What a deletion request targets. Only `user_data` is implemented; the
#: other two are declared so the vocabulary is stable, and are rejected at
#: the service boundary with an explicit "not implemented" error rather than
#: silently doing something partial. See `docs/DATA_LIFECYCLE.md`'s
#: "Deferred: pending product decision" section for what each would require.
DeletionScope = Literal["user_data", "user_account", "organization"]


class PlannedStep(BaseModel):
    """One category of data, what will happen to it, and how much there is.

    `count` is the number of rows the step will touch, discovered by
    counting before anything is mutated. It is `None` only when the count
    could not be determined without doing the work itself (nothing in the
    current implementation returns `None`, but the field allows a future
    step whose scope is genuinely unknowable up front rather than forcing
    it to report a misleading `0`).
    """

    model_config = ConfigDict(frozen=True)

    category: str
    action: DataAction
    table: str
    count: int | None
    #: Why this action and not another -- carried on the plan itself so a
    #: dry-run output is self-explaining, and so the justification lives
    #: next to the decision rather than only in documentation that can
    #: drift away from the code.
    rationale: str


class DeletionPlan(BaseModel):
    """The complete, read-only plan for one deletion request.

    Safe to compute and return without side effects -- this is what makes
    dry-run mode possible without a second code path.
    """

    model_config = ConfigDict(frozen=True)

    scope: DeletionScope
    target_user_id: uuid.UUID
    organization_id: uuid.UUID
    steps: list[PlannedStep] = Field(default_factory=list)

    @property
    def hard_delete_steps(self) -> list[PlannedStep]:
        return [s for s in self.steps if s.action == "hard_delete"]

    @property
    def anonymize_steps(self) -> list[PlannedStep]:
        return [s for s in self.steps if s.action == "anonymize"]

    @property
    def retain_steps(self) -> list[PlannedStep]:
        return [s for s in self.steps if s.action == "retain"]

    @property
    def total_rows_affected(self) -> int:
        """Rows that will actually be mutated -- retained categories
        excluded, since counting them here would overstate the blast radius
        of the operation."""
        return sum(
            step.count or 0 for step in self.steps if step.action in ("hard_delete", "anonymize")
        )


class StepResult(BaseModel):
    """What one planned step actually did.

    `succeeded=False` carries `error` and leaves `rows_affected` at whatever
    was confirmed before the failure -- never silently zeroed, since a step
    that deleted 40 of 100 rows before failing has genuinely changed state
    and a retry needs to know that.
    """

    model_config = ConfigDict(frozen=True)

    category: str
    action: DataAction
    succeeded: bool
    rows_affected: int = 0
    error: str | None = None


class DeletionResult(BaseModel):
    """The outcome of one execution.

    Deliberately carries no personal data about the deleted subject -- only
    the target's id, per-category counts, and status. The whole point of the
    operation is that the person's email/display name stop existing in this
    system; writing them into a deletion record "for the audit trail" would
    defeat it. `target_user_id` is retained because it is an opaque
    surrogate key, not personal data, and without it the record could not be
    correlated to anything at all.
    """

    model_config = ConfigDict(frozen=True)

    scope: DeletionScope
    target_user_id: uuid.UUID
    organization_id: uuid.UUID
    status: DeletionStatus
    steps: list[StepResult] = Field(default_factory=list)
    executed_at: datetime
    #: True when this run found nothing left to do because a previous run
    #: had already completed -- the observable signal that idempotent
    #: re-execution happened, rather than a silently identical response.
    was_noop: bool = False

    @property
    def failed_steps(self) -> list[StepResult]:
        return [s for s in self.steps if not s.succeeded]

    @property
    def total_rows_affected(self) -> int:
        return sum(step.rows_affected for step in self.steps)

    @property
    def anonymized_row_count(self) -> int:
        return sum(s.rows_affected for s in self.steps if s.action == "anonymize" and s.succeeded)

    @property
    def hard_deleted_row_count(self) -> int:
        return sum(s.rows_affected for s in self.steps if s.action == "hard_delete" and s.succeeded)
