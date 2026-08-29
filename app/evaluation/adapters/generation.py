"""The answer-generation and investigation "system under test" seams.

Split from `retrieval.py` because these produce a different shape of output
(generated text + citations, or evidence + hypotheses) and, in Mode 1, come
from a fixture *mapping* rather than a fixture *corpus* to search -- there is
no meaningful "rank a fixed answer against a query" operation the way there
is for retrieval, so the deterministic implementations here are simple
lookups by case id, not a scoring function.

`FixtureAnswerAdapter`/`FixtureInvestigationAdapter` intentionally do NOT
read their canned outputs from the dataset's own JSONL rows: a case's
`expected` block describes what a passing result must look like, not what
the system should be fed to produce it -- conflating the two would make a
dataset file simultaneously the test and the mocked implementation under
test, which defeats the point of grading anything. Canned outputs instead
live in `app.evaluation.fixtures.canned_generations`, keyed by case id,
authored to deliberately include both a case that should pass and one that
should fail each assertion type -- see that module.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from app.evaluation.schemas import EvaluationCase
from app.retrieval.schemas import ScoredChunk
from app.shared.schemas.agent_contracts import Citation, EvidenceItem, RootCauseHypothesis


@runtime_checkable
class AnswerAdapter(Protocol):
    async def generate_answer(
        self, case: EvaluationCase, chunks: list[ScoredChunk]
    ) -> tuple[str, list[Citation], float]:
        """Return `(answer_text, citations, confidence)` for `case`."""
        ...


@runtime_checkable
class InvestigationAdapter(Protocol):
    async def investigate(
        self, case: EvaluationCase, chunks: list[ScoredChunk]
    ) -> tuple[list[EvidenceItem], list[RootCauseHypothesis]]:
        """Return `(gathered_evidence, produced_hypotheses)` for `case`."""
        ...


@runtime_checkable
class CritiqueAdapter(Protocol):
    async def critique(self, case: EvaluationCase) -> tuple[str, str | None, int]:
        """Return `(review_status, critique_verdict, revision_count)` for
        `case` -- the same three fields `agents.investigation.critique.
        ReviewOutcome`/`InvestigationResult` carry (Priority 7)."""
        ...


class FixtureAnswerAdapter:
    def __init__(self, canned: dict[str, tuple[str, list[Citation], float]]) -> None:
        self._canned = canned

    async def generate_answer(
        self, case: EvaluationCase, chunks: list[ScoredChunk]
    ) -> tuple[str, list[Citation], float]:
        if case.id not in self._canned:
            raise KeyError(
                f"no canned answer fixture for case id {case.id!r} -- add one to "
                "app.evaluation.fixtures.canned_generations, or use a real AnswerAdapter"
            )
        return self._canned[case.id]


class FixtureInvestigationAdapter:
    def __init__(
        self, canned: dict[str, tuple[list[EvidenceItem], list[RootCauseHypothesis]]]
    ) -> None:
        self._canned = canned

    async def investigate(
        self, case: EvaluationCase, chunks: list[ScoredChunk]
    ) -> tuple[list[EvidenceItem], list[RootCauseHypothesis]]:
        if case.id not in self._canned:
            raise KeyError(
                f"no canned investigation fixture for case id {case.id!r} -- add one to "
                "app.evaluation.fixtures.canned_generations, or use a real InvestigationAdapter"
            )
        return self._canned[case.id]


class FixtureCritiqueAdapter:
    def __init__(self, canned: dict[str, tuple[str, str | None, int]]) -> None:
        self._canned = canned

    async def critique(self, case: EvaluationCase) -> tuple[str, str | None, int]:
        if case.id not in self._canned:
            raise KeyError(
                f"no canned critique fixture for case id {case.id!r} -- add one to "
                "app.evaluation.fixtures.canned_generations, or use a real CritiqueAdapter"
            )
        return self._canned[case.id]


class RealAnswerAdapter:
    """Mode 2/3: wraps the real `app.agents.service.answer_question` --
    same "code-complete, not exercised end-to-end in this repository's own
    development environment" status as `RealRetrievalAdapter`, for the same
    reason (no live database/`OPENAI_API_KEY` combination available here)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def generate_answer(
        self, case: EvaluationCase, chunks: list[ScoredChunk]
    ) -> tuple[str, list[Citation], float]:
        from app.agents import service as agents_service

        ask = await agents_service.answer_question(
            self._session, case.query, None, case.identity.to_identity(case.organization_uuid)
        )
        return (ask.answer or "", ask.citations, ask.confidence)


class RealInvestigationAdapter:
    """Mode 2/3: wraps the real `app.agents.service.triage_incident`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def investigate(
        self, case: EvaluationCase, chunks: list[ScoredChunk]
    ) -> tuple[list[EvidenceItem], list[RootCauseHypothesis]]:
        from app.agents import service as agents_service

        ask = await agents_service.answer_question(
            self._session, case.query, None, case.identity.to_identity(case.organization_uuid)
        )
        if ask.investigation is None:
            return ([], [])
        return (ask.investigation.evidence, ask.investigation.hypotheses)


class RealCritiqueAdapter:
    """Mode 2/3: wraps the real `app.agents.service.triage_incident`'s
    review fields (`InvestigationResult.review_status`/`critique_verdict`/
    `revision_count`, Priority 7). Same "code-complete, not exercised
    end-to-end in this repository's own development environment" status as
    every other `Real*Adapter` here."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def critique(self, case: EvaluationCase) -> tuple[str, str | None, int]:
        from app.agents import service as agents_service

        ask = await agents_service.answer_question(
            self._session, case.query, None, case.identity.to_identity(case.organization_uuid)
        )
        if ask.investigation is None:
            return ("not_reviewed", None, 0)
        investigation = ask.investigation
        return (
            investigation.review_status,
            investigation.critique_verdict,
            investigation.revision_count,
        )
