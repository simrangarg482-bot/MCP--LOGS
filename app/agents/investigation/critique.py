"""Bounded critique/reflection over Investigation Agent hypotheses
(Priority 7) -- the one place a generated hypothesis set is checked for
support, overconfidence, and (where the evidence itself makes it visible)
contradiction before being persisted as the investigation's result.

WHY THIS IS ADDITIONAL STEPS INSIDE THE EXISTING NODE, NOT NEW GRAPH NODES
    `agents.graph.build_investigation_graph` is one node
    (`investigation_agent`) whose Python closure already orchestrates
    multiple internal steps (gather evidence -> generate hypotheses ->
    attach to timeline) -- that is the established precedent in THIS
    codebase for a bounded, no-cycle sequence, not "one LangGraph node per
    logical step." `review_investigation` (below) is called from inside
    that same closure (`agents.investigation.node`), as one more bounded
    step, rather than as new graph nodes with conditional edges. This
    keeps the compiled graph itself structurally identical to today's (one
    entry node, one edge to `END`) -- there is no cycle construct at the
    graph level for a bug to turn into an infinite loop, because none
    exists at all.

THE TWO INVARIANTS THIS MODULE EXISTS TO PROTECT
    1. Critique output is never evidence. `_run_semantic_critique_call`
       gives the model ONLY the hypotheses and the evidence
       `agents.investigation.evidence.gather_evidence` already gathered
       (already authorization-scoped upstream -- this module fetches
       nothing new). Every evidence id the critique's own output
       references is validated against that same known set; an id it
       invents is dropped, never trusted (`_known_evidence_ids`,
       `_filter_contradictions`) -- the same "strip fabricated references,
       never surface them" discipline `investigation.hypothesis.
       _validate_hypotheses` already applies to hypothesis generation.
    2. A result must never claim to have been reviewed when review failed,
       was skipped, or could not validate its own output.
       `ReviewOutcome.review_status` is the one field that draws this line
       explicitly -- see its own docstring on `InvestigationResult`.

BOUNDS ARE CODE CONSTANTS, NOT SETTINGS
    `MAX_CRITIQUE_PASSES`/`MAX_REVISION_ATTEMPTS` are module-level
    constants, deliberately NOT `Settings` fields -- so no configuration
    change (accidental or malicious) can widen them into an unbounded
    loop. `review_investigation`'s control flow is a fixed, linear sequence
    of `if`/`else` branches with no loop construct at all (not a `while`
    reading a counter) -- structurally, not just by configured value,
    incapable of iterating more than the two critique calls and one
    revision call this module ever makes.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
    - No contradiction *engine*: the only contradiction signal is what the
      semantic critique itself observes directly in the evidence text it
      was handed, reported as `evidence_ids` pointing at real, already-
      authorized evidence -- there is no independent structural mechanism
      that compares evidence pairwise for conflict (repository discovery
      found no trustworthy data shape to build one on; see
      `docs/INVESTIGATION_CRITIQUE.md`).
    - No hidden reasoning is persisted. `revision_guidance` (the one
      critique-authored prose field) is used ONLY to steer the single
      revision attempt's prompt, in memory, and is never written to
      `critique_issues` or anywhere persisted -- only short, structured
      category tags are.
    - No confidence value is trusted from the model. `confidence`
      penalties applied on a flagged hypothesis are fixed, code-defined
      constants (`_OVERCONFIDENCE_PENALTY`/`_UNSUPPORTED_CLAIM_PENALTY`),
      the same "small, explicit, documented weights" precedent
      `agents.confidence._SIGNAL_WEIGHTS` already sets -- never a
      model-supplied delta.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, ConfigDict, Field

from app.agents.investigation.hypothesis import generate_hypotheses
from app.agents.prompt_safety import build_messages
from app.agents.retry import call_with_retry
from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings
from app.shared.schemas import EvidenceItem, RootCauseHypothesis

logger = get_logger(__name__)

#: Hard ceiling on how many semantic critique LLM calls one investigation
#: may ever trigger: one initial pass, and (only if that pass says
#: "revise") one more pass validating the single revision attempt. A code
#: constant, not a setting -- see module docstring.
MAX_CRITIQUE_PASSES = 2
#: Hard ceiling on how many times hypotheses may be regenerated in response
#: to critique feedback. A second "revise" verdict (on the already-revised
#: set) is treated as "reject", never as a second revision -- see
#: `review_investigation`'s pass-2 branch.
MAX_REVISION_ATTEMPTS = 1

#: Fixed, documented confidence penalties -- never a model-supplied delta.
#: Mirrors `agents.confidence._SIGNAL_WEIGHTS`'s "small, explicit, code-
#: defined constant" precedent for a deterministic adjustment.
_OVERCONFIDENCE_PENALTY = 0.2
_UNSUPPORTED_CLAIM_PENALTY = 0.3

_MAX_REVISION_GUIDANCE_CHARS = 500

_REJECTED_NEXT_STEPS = [
    "Automated critique found the generated hypothesis/hypotheses "
    "insufficiently supported by the available evidence. Recommend manual "
    "investigation of the evidence listed below."
]


class _CritiqueParsingError(Exception):
    """Raised when the critique model's response isn't valid, expected-
    shape JSON. Caught by `agents.retry.call_with_retry`'s caller
    (`_run_semantic_critique`), mirroring `investigation.hypothesis.
    _HypothesisParsingError`'s identical role."""


class ContradictionNote(BaseModel):
    """One contradiction the critique observed directly in the evidence it
    was given -- never a claim about evidence it wasn't shown. `evidence_ids`
    is validated against the known evidence set before this is ever kept
    (see `_filter_contradictions`)."""

    model_config = ConfigDict(frozen=True)

    evidence_ids: list[str]
    detail: str


class SemanticCritiqueResult(BaseModel):
    """Parsed, validated output of one semantic critique LLM call."""

    model_config = ConfigDict(frozen=True)

    verdict: Literal["accept", "revise", "reject"]
    unsupported_hypothesis_indices: list[int] = Field(default_factory=list)
    contradictory_evidence: list[ContradictionNote] = Field(default_factory=list)
    #: Ephemeral only -- used to steer the one bounded revision prompt, in
    #: memory. Never persisted (see module docstring).
    revision_guidance: str | None = None


class ReviewOutcome(BaseModel):
    """What `review_investigation` hands back to `investigation.node` --
    the FINAL hypotheses/owner-team/next-steps to persist (already
    reflecting any revision), plus the review metadata that becomes
    `InvestigationResult.review_status`/`critique_verdict`/`revision_count`/
    `critique_issues`.
    """

    model_config = ConfigDict(frozen=True)

    hypotheses: list[RootCauseHypothesis]
    suggested_owner_team: str | None
    suggested_next_steps: list[str]
    review_status: Literal["not_reviewed", "reviewed", "review_failed"]
    critique_verdict: Literal["accept", "revise", "reject"] | None
    revision_count: int
    critique_issues: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# deterministic structural validation -- no LLM call
# --------------------------------------------------------------------------


def validate_structurally(
    hypotheses: list[RootCauseHypothesis],
    evidence: list[EvidenceItem],
    *,
    min_evidence_count: int,
    overconfidence_threshold: float,
    min_evidence_per_hypothesis: int,
) -> list[str]:
    """Objective, deterministic checks that need no model call.

    Two dimensions -- see `docs/INVESTIGATION_CRITIQUE.md` for why these
    two, and not more: every hypothesis reaching this function already has
    100% real (non-fabricated) `supporting_evidence_ids` -- `investigation.
    hypothesis._validate_hypotheses` strips fake references before a
    hypothesis is ever constructed, so a "does this citation exist" check
    here would be redundant. What is NOT already checked anywhere:

      `"insufficient_information"` -- the whole evidence base is thin
        (`len(evidence) < min_evidence_count`), regardless of what any
        individual hypothesis claims.
      `"overconfidence:hypothesis_<i>"` -- a hypothesis asserts confidence
        at or above `overconfidence_threshold` while citing fewer than
        `min_evidence_per_hypothesis` pieces of evidence -- a self-reported
        number unsupported by its own citation breadth.
    """
    issues: list[str] = []
    if len(evidence) < min_evidence_count:
        issues.append("insufficient_information")

    for index, hypothesis in enumerate(hypotheses):
        if (
            hypothesis.confidence >= overconfidence_threshold
            and len(hypothesis.supporting_evidence_ids) < min_evidence_per_hypothesis
        ):
            issues.append(f"overconfidence:hypothesis_{index}")

    return issues


def _forced_verdict(
    structural_issues: list[str], hypothesis_count: int
) -> Literal["reject"] | None:
    """`"reject"` when a structural issue alone is disqualifying (thin
    evidence base, or nothing to critique at all); `None` otherwise, meaning
    the semantic critique pass decides."""
    if "insufficient_information" in structural_issues or hypothesis_count == 0:
        return "reject"
    return None


# --------------------------------------------------------------------------
# semantic critique -- one bounded LLM call
# --------------------------------------------------------------------------


def _known_evidence_ids(evidence: list[EvidenceItem]) -> set[str]:
    return {item.reference for item in evidence}


def _build_hypothesis_block(hypotheses: list[RootCauseHypothesis]) -> str:
    lines = []
    for index, hypothesis in enumerate(hypotheses):
        cited = ", ".join(hypothesis.supporting_evidence_ids)
        lines.append(
            f"Hypothesis {index}: {hypothesis.description}\n"
            f"  self-reported confidence: {hypothesis.confidence:.2f}\n"
            f"  cites: [{cited}]"
        )
    return "\n\n".join(lines)


def _build_evidence_block(evidence: list[EvidenceItem]) -> str:
    """Same rendering convention as `investigation.hypothesis.
    _build_evidence_block` -- the critique must see evidence keyed by the
    identical reference strings hypotheses already cite."""
    lines = []
    for item in evidence:
        lines.append(f"[{item.reference}] ({item.source}): {item.summary}")
    return "\n\n".join(lines)


async def _run_semantic_critique_call(
    llm: BaseChatModel,
    query: str,
    hypotheses: list[RootCauseHypothesis],
    evidence: list[EvidenceItem],
    structural_issues: list[str],
) -> SemanticCritiqueResult:
    """One LLM call. No tools, no retrieval -- the model receives exactly
    the hypotheses and evidence already in memory, nothing else (see module
    docstring's first invariant)."""
    hypothesis_block = _build_hypothesis_block(hypotheses)
    evidence_block = _build_evidence_block(evidence)
    structural_note = (
        f"Deterministic pre-checks already flagged: {', '.join(structural_issues)}.\n\n"
        if structural_issues
        else ""
    )

    messages = build_messages(
        system_instructions=(
            "You are critiquing root-cause hypotheses for an incident investigation. "
            "You are given the hypotheses and the ONLY evidence that was gathered for "
            "this investigation. Do not use outside knowledge and do not invent facts "
            "or evidence that isn't listed below.\n\n"
            f"{structural_note}"
            "Evaluate whether each hypothesis's claim is actually supported by the "
            "CONTENT of the evidence it cites (not merely that the citation exists), "
            "and whether any of the evidence appears to conflict with another piece "
            "of evidence.\n\n"
            "Respond with ONLY a single JSON object (no markdown code fences, no "
            "commentary) with exactly this shape:\n"
            '{"verdict": "accept" | "revise" | "reject", '
            '"unsupported_hypothesis_indices": [0, ...], '
            '"contradictory_evidence": [{"evidence_ids": ["...", "..."], "detail": '
            '"..."}], '
            '"revision_guidance": "short actionable instruction, or null"}\n\n'
            'Rules: "accept" means every hypothesis is adequately supported. '
            '"revise" means at least one hypothesis needs a concrete, fixable change '
            "-- provide `revision_guidance` describing exactly what to fix. "
            '"reject" means the evidence does not support a confident conclusion at '
            "all. `unsupported_hypothesis_indices` lists the integer index (from the "
            "Hypothesis N labels below) of any hypothesis whose claim is not actually "
            "backed by its cited evidence's content. Every `evidence_ids` entry in "
            "`contradictory_evidence` MUST be copied verbatim from one of the "
            "bracketed reference strings below -- never invent one."
        ),
        evidence_block=f"{hypothesis_block}\n\n---\n\n{evidence_block}",
        task=f"Incident: {query}",
    )
    response = await llm.ainvoke(messages)
    raw_text = str(response.content).strip()

    parsed = _parse_response(raw_text)
    if parsed is None:
        raise _CritiqueParsingError(f"critique response was not valid JSON: {raw_text[:200]!r}")

    return _validate_critique_result(parsed, known_evidence_ids=_known_evidence_ids(evidence))


def _parse_response(raw_text: str) -> dict[str, Any] | None:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[len("json") :].strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("investigation_critique_parse_failed", error=str(exc))
        return None
    if not isinstance(parsed, dict):
        logger.warning("investigation_critique_unexpected_shape", raw_type=type(parsed).__name__)
        return None
    return parsed


def _validate_critique_result(
    parsed: dict[str, Any], *, known_evidence_ids: set[str]
) -> SemanticCritiqueResult:
    """Structural validation of the critique's OWN output -- malformed
    output must raise, never be silently treated as "accept" (this
    priority's explicit requirement)."""
    verdict = parsed.get("verdict")
    if verdict not in ("accept", "revise", "reject"):
        raise _CritiqueParsingError(f"invalid critique verdict: {verdict!r}")

    raw_indices = parsed.get("unsupported_hypothesis_indices", [])
    if not isinstance(raw_indices, list):
        raise _CritiqueParsingError("unsupported_hypothesis_indices was not a list")
    indices = [i for i in raw_indices if isinstance(i, int) and not isinstance(i, bool) and i >= 0]

    contradictions = _filter_contradictions(
        parsed.get("contradictory_evidence", []), known_evidence_ids
    )

    guidance = parsed.get("revision_guidance")
    if isinstance(guidance, str) and guidance.strip():
        guidance = guidance.strip()[:_MAX_REVISION_GUIDANCE_CHARS]
    else:
        guidance = None

    return SemanticCritiqueResult(
        verdict=verdict,
        unsupported_hypothesis_indices=indices,
        contradictory_evidence=contradictions,
        revision_guidance=guidance,
    )


def _filter_contradictions(raw: Any, known_evidence_ids: set[str]) -> list[ContradictionNote]:
    """Drop any note referencing an evidence id that doesn't exist (a
    fabricated reference is exactly as untrustworthy here as it is in
    `investigation.hypothesis._validate_hypotheses`), and any note left
    with fewer than 2 real ids -- a contradiction needs two things to
    conflict."""
    if not isinstance(raw, list):
        return []
    notes: list[ContradictionNote] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        raw_ids = entry.get("evidence_ids")
        detail = entry.get("detail")
        if not isinstance(raw_ids, list) or not isinstance(detail, str) or not detail.strip():
            continue
        real_ids = [ref for ref in raw_ids if isinstance(ref, str) and ref in known_evidence_ids]
        if len(real_ids) < 2:
            continue
        notes.append(ContradictionNote(evidence_ids=real_ids, detail=detail.strip()))
    return notes


async def _run_semantic_critique(
    llm: BaseChatModel,
    query: str,
    hypotheses: list[RootCauseHypothesis],
    evidence: list[EvidenceItem],
    structural_issues: list[str],
    retry_count: dict[str, int],
    *,
    node_name: str,
) -> SemanticCritiqueResult | None:
    """`None` on failure (never raises) -- the caller
    (`review_investigation`) turns that into `review_status="review_failed"`,
    never a silent "accept"."""
    try:
        return await call_with_retry(
            node_name,
            lambda: _run_semantic_critique_call(
                llm, query, hypotheses, evidence, structural_issues
            ),
            retry_count=retry_count,
        )
    except Exception as exc:  # noqa: BLE001 -- degrade to "critique unavailable", never propagate
        logger.warning("investigation_critique_exhausted", query=query, error=str(exc))
        return None


_OVERCONFIDENT_TAG_PREFIX = "overconfidence:hypothesis_"


def _overconfident_indices(structural_issues: list[str]) -> set[int]:
    indices = set()
    for tag in structural_issues:
        if tag.startswith(_OVERCONFIDENT_TAG_PREFIX):
            indices.add(int(tag[len(_OVERCONFIDENT_TAG_PREFIX) :]))
    return indices


def _apply_penalties(
    hypotheses: list[RootCauseHypothesis],
    structural_issues: list[str],
    semantic: SemanticCritiqueResult,
) -> list[RootCauseHypothesis]:
    """Apply the fixed, documented confidence penalties to flagged
    hypotheses -- never a model-supplied delta (see module docstring). Both
    penalties can stack on the same hypothesis (flagged overconfident AND
    semantically unsupported), clamped at 0.0."""
    overconfident = _overconfident_indices(structural_issues)
    adjusted: list[RootCauseHypothesis] = []
    for index, hypothesis in enumerate(hypotheses):
        penalty = 0.0
        if index in overconfident:
            penalty += _OVERCONFIDENCE_PENALTY
        if index in semantic.unsupported_hypothesis_indices:
            penalty += _UNSUPPORTED_CLAIM_PENALTY
        if penalty:
            hypothesis = hypothesis.model_copy(
                update={"confidence": max(0.0, hypothesis.confidence - penalty)}
            )
        adjusted.append(hypothesis)
    return adjusted


def _issue_tags(structural_issues: list[str], semantic: SemanticCritiqueResult) -> list[str]:
    tags = list(structural_issues)
    tags += [f"unsupported_claim:hypothesis_{i}" for i in semantic.unsupported_hypothesis_indices]
    if semantic.contradictory_evidence:
        tags.append("contradictory_evidence")
    return tags


# --------------------------------------------------------------------------
# orchestration -- the one entry point `investigation.node` calls
# --------------------------------------------------------------------------


async def review_investigation(
    llm: BaseChatModel,
    query: str,
    evidence: list[EvidenceItem],
    hypotheses: list[RootCauseHypothesis],
    suggested_owner_team: str | None,
    suggested_next_steps: list[str],
    retry_count: dict[str, int],
) -> ReviewOutcome:
    """Bounded critique + at most one revision, returning the FINAL
    hypotheses/owner-team/next-steps to persist plus review metadata.

    Called only when there is something to critique: `investigation.node`
    skips this entirely when evidence or hypothesis generation already
    produced nothing (both cases already degrade honestly on their own,
    and there is nothing for critique to evaluate). `review_status` stays
    `"not_reviewed"` in that case, and in that case alone besides the
    explicit `investigation_critique_enabled=False` kill switch.

    Structurally bounded: this function contains no loop of any kind --
    reading it top to bottom is reading its complete, worst-case call
    sequence (one structural check, one semantic critique call, optionally
    one revision call, one more structural check, one more semantic
    critique call). See module docstring for why a loop construct was never
    introduced in the first place.
    """
    settings = get_settings()
    if not settings.investigation_critique_enabled or not hypotheses:
        return ReviewOutcome(
            hypotheses=hypotheses,
            suggested_owner_team=suggested_owner_team,
            suggested_next_steps=suggested_next_steps,
            review_status="not_reviewed",
            critique_verdict=None,
            revision_count=0,
            critique_issues=[],
        )

    def _validate(current: list[RootCauseHypothesis]) -> list[str]:
        return validate_structurally(
            current,
            evidence,
            min_evidence_count=settings.investigation_critique_min_evidence_count,
            overconfidence_threshold=settings.investigation_critique_overconfidence_threshold,
            min_evidence_per_hypothesis=settings.investigation_critique_min_evidence_per_hypothesis,
        )

    # --- pass 1 -----------------------------------------------------------
    structural_1 = _validate(hypotheses)
    if _forced_verdict(structural_1, len(hypotheses)) == "reject":
        return _rejected(structural_1, revision_count=0)

    semantic_1 = await _run_semantic_critique(
        llm,
        query,
        hypotheses,
        evidence,
        structural_1,
        retry_count,
        node_name="investigation_agent.critique",
    )
    if semantic_1 is None:
        return ReviewOutcome(
            hypotheses=hypotheses,
            suggested_owner_team=suggested_owner_team,
            suggested_next_steps=suggested_next_steps,
            review_status="review_failed",
            critique_verdict=None,
            revision_count=0,
            critique_issues=structural_1,
        )

    issues_1 = _issue_tags(structural_1, semantic_1)
    if semantic_1.verdict == "reject":
        return _rejected(issues_1, revision_count=0)
    if semantic_1.verdict == "accept":
        return ReviewOutcome(
            hypotheses=_apply_penalties(hypotheses, structural_1, semantic_1),
            suggested_owner_team=suggested_owner_team,
            suggested_next_steps=suggested_next_steps,
            review_status="reviewed",
            critique_verdict="accept",
            revision_count=0,
            critique_issues=issues_1,
        )

    # --- verdict == "revise": the ONE bounded revision attempt ------------
    try:
        revised, revised_owner_team, revised_next_steps = await call_with_retry(
            "investigation_agent.revision",
            lambda: generate_hypotheses(
                llm, query, evidence, critique_feedback=semantic_1.revision_guidance
            ),
            retry_count=retry_count,
        )
    except Exception as exc:  # noqa: BLE001 -- revision failure degrades, never propagates
        logger.warning("investigation_revision_exhausted", query=query, error=str(exc))
        return ReviewOutcome(
            hypotheses=hypotheses,
            suggested_owner_team=suggested_owner_team,
            suggested_next_steps=suggested_next_steps,
            review_status="review_failed",
            critique_verdict="revise",
            revision_count=0,
            critique_issues=issues_1,
        )

    if not revised:
        return _rejected(issues_1 + ["revision_produced_no_hypotheses"], revision_count=1)

    # --- pass 2: validate the revision, no further revision regardless ---
    structural_2 = _validate(revised)
    issues_2 = issues_1 + structural_2
    if _forced_verdict(structural_2, len(revised)) == "reject":
        return _rejected(issues_2, revision_count=1)

    semantic_2 = await _run_semantic_critique(
        llm,
        query,
        revised,
        evidence,
        structural_2,
        retry_count,
        node_name="investigation_agent.critique_revision",
    )
    if semantic_2 is None:
        return ReviewOutcome(
            hypotheses=revised,
            suggested_owner_team=revised_owner_team,
            suggested_next_steps=revised_next_steps,
            review_status="review_failed",
            critique_verdict="revise",
            revision_count=1,
            critique_issues=issues_2,
        )

    issues_2 = _issue_tags(structural_2, semantic_2) + issues_1
    # A second "revise" verdict has nowhere left to go -- MAX_REVISION_ATTEMPTS
    # is exhausted. Treated as reject, not a silent accept: the critique
    # itself just said this revision still isn't right.
    if semantic_2.verdict in ("revise", "reject"):
        return _rejected(issues_2, revision_count=1)

    return ReviewOutcome(
        hypotheses=_apply_penalties(revised, structural_2, semantic_2),
        suggested_owner_team=revised_owner_team,
        suggested_next_steps=revised_next_steps,
        review_status="reviewed",
        critique_verdict="accept",
        revision_count=1,
        critique_issues=issues_2,
    )


def _rejected(issues: list[str], *, revision_count: int) -> ReviewOutcome:
    return ReviewOutcome(
        hypotheses=[],
        suggested_owner_team=None,
        suggested_next_steps=_REJECTED_NEXT_STEPS,
        review_status="reviewed",
        critique_verdict="reject",
        revision_count=revision_count,
        critique_issues=issues,
    )
