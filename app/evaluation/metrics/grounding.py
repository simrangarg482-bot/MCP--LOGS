"""Deterministic grounding and citation checks -- no LLM call required.

These check the same *kind* of thing `app.agents.answer.grounding.
verify_grounding` checks (is a claim traceable to retrieved evidence), but
independently and by a cheaper, purely lexical method -- exactly the
"default evaluation suite must run without paid APIs" requirement. Using
EKIP's own `verify_grounding` here would be grading its homework with its
own answer key (the same reasoning `tests/rag_validation/README.md`'s
"Why the judge is separate from EKIP's own grounding check" section already
gives for its own, separate LLM judge). A semantic/LLM-based grounding
evaluator can be added later as another function in this module or a new
adapter -- see `app.evaluation.adapters.llm` for the seam.
"""

from __future__ import annotations

import uuid

from app.evaluation.schemas import CitationExpectation, normalize_text
from app.shared.schemas.agent_contracts import Citation


def check_required_concepts(text: str, required_concepts: list[str]) -> list[str]:
    """Return the subset of `required_concepts` NOT found (case/whitespace-
    insensitive substring match) in `text`. Empty list means every required
    concept was present.
    """
    normalized_text = normalize_text(text)
    return [
        concept for concept in required_concepts if normalize_text(concept) not in normalized_text
    ]


def check_forbidden_concepts(text: str, forbidden_concepts: list[str]) -> list[str]:
    """Return the subset of `forbidden_concepts` that WERE found in `text`
    (i.e. violations) -- empty list means clean.
    """
    normalized_text = normalize_text(text)
    return [
        concept for concept in forbidden_concepts if normalize_text(concept) in normalized_text
    ]


def concepts_traceable_to_evidence(
    required_concepts: list[str], evidence_texts: list[str]
) -> dict[str, bool]:
    """For each of `required_concepts`, whether it appears (substring,
    normalized) in at least one of `evidence_texts` -- "can this required
    concept be traced to *retrieved* evidence," not just "does it appear in
    the final answer text" (that's `check_required_concepts`'s job, against
    the answer). A concept present in the answer but absent from every piece
    of evidence is exactly the shape of an unsupported claim.
    """
    normalized_evidence = [normalize_text(t) for t in evidence_texts]
    return {
        concept: any(normalize_text(concept) in evidence for evidence in normalized_evidence)
        for concept in required_concepts
    }


class CitationCheckResult:
    """Structured outcome of `check_citations`, kept as a small dataclass-ish
    object (not a bare dict) so the runner and its tests can assert on named
    fields instead of string keys.
    """

    def __init__(
        self,
        *,
        count_satisfied: bool,
        unresolved_citations: list[str],
        unsupported_citations: list[str],
    ) -> None:
        self.count_satisfied = count_satisfied
        self.unresolved_citations = unresolved_citations
        self.unsupported_citations = unsupported_citations

    @property
    def passed(self) -> bool:
        return (
            self.count_satisfied
            and not self.unresolved_citations
            and not self.unsupported_citations
        )


def check_citations(
    citations: list[Citation],
    retrieved_chunk_ids: set[uuid.UUID],
    retrieved_chunk_contents: dict[uuid.UUID, str],
    expectation: CitationExpectation,
) -> CitationCheckResult:
    """Verify `citations` (as an Answer Agent would actually produce, per
    `app.shared.schemas.agent_contracts.Citation`) against `expectation` and
    the chunks that were genuinely retrieved for this case.

    Three checks, matching this package's spec:
      1. Count -- at least `expectation.minimum` citations exist.
      2. Resolution -- every citation's `chunk_id` is one of the chunks that
         was actually retrieved (a citation pointing at a chunk id outside
         the retrieved set is fabricated, structurally, regardless of its
         excerpt text).
      3. Support -- when `expectation.must_support_answer`, each citation's
         `excerpt` is genuinely a substring of that chunk's real content
         (normalized) -- catches an excerpt that was paraphrased/invented
         rather than quoted from the actual retrieved text.
    """
    count_satisfied = len(citations) >= expectation.minimum

    unresolved: list[str] = []
    unsupported: list[str] = []
    for citation in citations:
        citation_label = str(citation.chunk_id)
        if citation.chunk_id not in retrieved_chunk_ids:
            unresolved.append(citation_label)
            continue
        if expectation.must_support_answer:
            chunk_content = retrieved_chunk_contents.get(citation.chunk_id, "")
            if normalize_text(citation.excerpt) not in normalize_text(chunk_content):
                unsupported.append(citation_label)

    return CitationCheckResult(
        count_satisfied=count_satisfied,
        unresolved_citations=unresolved,
        unsupported_citations=unsupported,
    )
