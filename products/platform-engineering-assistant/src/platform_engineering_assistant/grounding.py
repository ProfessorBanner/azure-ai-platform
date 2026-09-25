"""The fail-closed grounding policy: enforced OUTSIDE the model.

WHAT THIS PROVES, AND WHAT IT DOES NOT
--------------------------------------
This enforces **citation containment and structural grounding**: every citation
names a chunk that was actually retrieved for this question, an answered
response really does carry an answer and at least one citation, and a refusal
really does carry neither.

It does NOT prove semantic entailment. Nothing here checks that the cited chunk
*supports* the sentence it is attached to; a model can cite a real chunk and
still say something the chunk does not license. Establishing that is the
evaluation phase's job, and claiming it here would be the more dangerous error,
because a containment check is easy to mistake for a correctness check.

FAIL CLOSED
-----------
Every violation produces a refusal or a typed failure. None produces an answer.
The reasoning is asymmetric: a wrongly refused question costs a person some
time; a confidently wrong answer with a real-looking citation is believed. When
the two are traded against each other, refusing is always the cheaper mistake.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from platform_engineering_assistant.domain import Citation, RefusalReason
from platform_engineering_assistant.errors import AssistantError, FailureCategory
from platform_engineering_assistant.generation.draft import DraftDisposition, GroundedDraft
from platform_engineering_assistant.retrieval.index import RetrievalResult


class GroundingError(AssistantError):
    """A draft could not be grounded and no safe answer could be derived."""

    category = FailureCategory.UNSUPPORTED_CITATION


class GroundingViolation(StrEnum):
    """Why a draft failed the policy. Recorded in telemetry, never shown raw."""

    CITATION_NOT_RETRIEVED = "citation_not_retrieved"
    DUPLICATE_CITATION = "duplicate_citation"
    EMPTY_CITATION_ID = "empty_citation_id"
    ANSWER_WITHOUT_CITATION = "answer_without_citation"
    ANSWER_WITHOUT_TEXT = "answer_without_text"
    ANSWER_WITH_REFUSAL_REASON = "answer_with_refusal_reason"
    REFUSAL_WITH_ANSWER = "refusal_with_answer"
    REFUSAL_WITH_CITATIONS = "refusal_with_citations"
    REFUSAL_WITHOUT_REASON = "refusal_without_reason"


ALLOWED_REFUSAL_REASONS = frozenset(RefusalReason)


@dataclass(frozen=True, slots=True)
class GroundingDecision:
    """The server's verdict on a draft.

    `citations` are built by the server from trusted chunk metadata; the model's
    contribution is only the set of chunk ids, and only after they are checked.
    """

    accepted: bool
    citations: tuple[Citation, ...] = ()
    refusal_reason: RefusalReason | None = None
    violation: GroundingViolation | None = None


def _build_citations(
    chunk_ids: list[str], retrieved: list[RetrievalResult]
) -> tuple[Citation, ...]:
    """Construct citations from TRUSTED chunk metadata, in retrieval order.

    The model supplies identifiers and nothing else. Paths, titles, headings and
    scores are read from the retrieved chunks, so a fabricated path cannot reach
    a response even if the model invents one — it has nowhere to put it.

    Ordering follows retrieval rank rather than the order the model happened to
    cite in, which makes the output deterministic and independent of the model.
    """
    wanted = set(chunk_ids)
    return tuple(
        Citation(
            chunk_id=result.chunk.chunk_id,
            doc_id=result.chunk.doc_id,
            doc_path=result.chunk.doc_path,
            heading_path=result.chunk.heading_path,
            score=result.score,
        )
        for result in retrieved
        if result.chunk.chunk_id in wanted
    )


def enforce_grounding(draft: GroundedDraft, retrieved: list[RetrievalResult]) -> GroundingDecision:
    """Apply the policy. Pure: no I/O, no logging, no model involvement."""
    retrieved_ids = {result.chunk.chunk_id for result in retrieved}
    cited = draft.cited_chunk_ids

    # --- citation integrity, checked for BOTH dispositions ------------------
    if any(not chunk_id.strip() for chunk_id in cited):
        return _refuse(GroundingViolation.EMPTY_CITATION_ID)
    if len(set(cited)) != len(cited):
        # A repeated id is not a stylistic quirk; it means the draft was not
        # produced from a clean reading of the evidence, so it is not trusted.
        return _refuse(GroundingViolation.DUPLICATE_CITATION)
    unknown = [chunk_id for chunk_id in cited if chunk_id not in retrieved_ids]
    if unknown:
        # The model cited something it was never given. This is the single most
        # important check in the product.
        return _refuse(GroundingViolation.CITATION_NOT_RETRIEVED)

    # --- disposition consistency --------------------------------------------
    if draft.disposition is DraftDisposition.ANSWERED:
        if draft.answer is None or not draft.answer.strip():
            return _refuse(GroundingViolation.ANSWER_WITHOUT_TEXT)
        if not cited:
            return _refuse(GroundingViolation.ANSWER_WITHOUT_CITATION)
        if draft.refusal_reason is not None:
            return _refuse(GroundingViolation.ANSWER_WITH_REFUSAL_REASON)
        return GroundingDecision(accepted=True, citations=_build_citations(cited, retrieved))

    if draft.answer is not None and draft.answer.strip():
        return _refuse(GroundingViolation.REFUSAL_WITH_ANSWER)
    if cited:
        return _refuse(GroundingViolation.REFUSAL_WITH_CITATIONS)
    if draft.refusal_reason is None or draft.refusal_reason not in ALLOWED_REFUSAL_REASONS:
        return _refuse(GroundingViolation.REFUSAL_WITHOUT_REASON)

    # An honest refusal: passed through as the model's own stated reason.
    return GroundingDecision(accepted=False, refusal_reason=draft.refusal_reason)


def _refuse(violation: GroundingViolation) -> GroundingDecision:
    """Turn any policy violation into a safe refusal.

    The public reason is always `unsupported_citation`: the caller learns the
    answer was not grounded, not which specific way the model misbehaved, which
    would be a hint worth probing.
    """
    return GroundingDecision(
        accepted=False,
        refusal_reason=RefusalReason.UNSUPPORTED_CITATION,
        violation=violation,
    )
