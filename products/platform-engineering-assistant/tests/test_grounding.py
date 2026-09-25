"""The fail-closed grounding policy.

This is the product's central safety property: a model may influence what is
said and which of the supplied chunks support it, and nothing else.
"""

from __future__ import annotations

import pytest

from platform_engineering_assistant.domain import RefusalReason
from platform_engineering_assistant.generation.draft import DraftDisposition, GroundedDraft
from platform_engineering_assistant.grounding import (
    GroundingViolation,
    enforce_grounding,
)
from tests.generation_fakes import RETRIEVED

ANSWER = "Terraform state is separated per environment."


def draft(**kwargs: object) -> GroundedDraft:
    return GroundedDraft.model_validate(kwargs)


# --- the accepted path ------------------------------------------------------


def test_a_well_formed_answered_draft_is_accepted() -> None:
    decision = enforce_grounding(
        draft(
            disposition=DraftDisposition.ANSWERED,
            answer=ANSWER,
            cited_chunk_ids=["a::decision::0::0"],
        ),
        RETRIEVED,
    )
    assert decision.accepted is True
    assert [citation.chunk_id for citation in decision.citations] == ["a::decision::0::0"]


def test_citations_are_built_from_trusted_metadata_not_from_the_model() -> None:
    """The model supplies ids; paths, headings and scores come from the corpus."""
    decision = enforce_grounding(
        draft(
            disposition=DraftDisposition.ANSWERED,
            answer=ANSWER,
            cited_chunk_ids=["b::context::0::0"],
        ),
        RETRIEVED,
    )
    citation = decision.citations[0]
    assert citation.doc_id == "adr-0002"
    assert citation.doc_path == "docs/adr/0002-x.md"
    assert citation.heading_path == "Decision"
    assert citation.score == 6.0


def test_citations_follow_retrieval_order_not_the_order_the_model_cited() -> None:
    """Output ordering must be server-owned and therefore deterministic."""
    decision = enforce_grounding(
        draft(
            disposition=DraftDisposition.ANSWERED,
            answer=ANSWER,
            cited_chunk_ids=["c::status::0::0", "a::decision::0::0"],
        ),
        RETRIEVED,
    )
    assert [c.chunk_id for c in decision.citations] == ["a::decision::0::0", "c::status::0::0"]


def test_an_honest_refusal_passes_through_with_its_reason() -> None:
    decision = enforce_grounding(
        draft(
            disposition=DraftDisposition.REFUSED,
            refusal_reason=RefusalReason.INSUFFICIENT_EVIDENCE,
        ),
        RETRIEVED,
    )
    assert decision.accepted is False
    assert decision.refusal_reason is RefusalReason.INSUFFICIENT_EVIDENCE
    assert decision.violation is None


# --- fail-closed violations -------------------------------------------------


def test_a_citation_outside_the_retrieved_set_fails_closed() -> None:
    """The single most important check in the product."""
    decision = enforce_grounding(
        draft(
            disposition=DraftDisposition.ANSWERED,
            answer=ANSWER,
            cited_chunk_ids=["fabricated::invented::0::0"],
        ),
        RETRIEVED,
    )
    assert decision.accepted is False
    assert decision.violation is GroundingViolation.CITATION_NOT_RETRIEVED
    assert decision.refusal_reason is RefusalReason.UNSUPPORTED_CITATION
    assert decision.citations == ()


def test_a_real_id_mixed_with_a_fabricated_one_still_fails_closed() -> None:
    """Partial grounding is not grounding."""
    decision = enforce_grounding(
        draft(
            disposition=DraftDisposition.ANSWERED,
            answer=ANSWER,
            cited_chunk_ids=["a::decision::0::0", "fabricated::invented::0::0"],
        ),
        RETRIEVED,
    )
    assert decision.accepted is False
    assert decision.violation is GroundingViolation.CITATION_NOT_RETRIEVED


def test_an_answer_with_no_citation_fails_closed() -> None:
    decision = enforce_grounding(
        draft(disposition=DraftDisposition.ANSWERED, answer=ANSWER), RETRIEVED
    )
    assert decision.accepted is False
    assert decision.violation is GroundingViolation.ANSWER_WITHOUT_CITATION


def test_duplicate_citations_fail_closed() -> None:
    decision = enforce_grounding(
        draft(
            disposition=DraftDisposition.ANSWERED,
            answer=ANSWER,
            cited_chunk_ids=["a::decision::0::0", "a::decision::0::0"],
        ),
        RETRIEVED,
    )
    assert decision.accepted is False
    assert decision.violation is GroundingViolation.DUPLICATE_CITATION


def test_an_empty_citation_id_fails_closed() -> None:
    decision = enforce_grounding(
        draft(disposition=DraftDisposition.ANSWERED, answer=ANSWER, cited_chunk_ids=["   "]),
        RETRIEVED,
    )
    assert decision.accepted is False
    assert decision.violation is GroundingViolation.EMPTY_CITATION_ID


@pytest.mark.parametrize("answer", [None, "", "   "])
def test_an_answered_draft_without_text_fails_closed(answer: str | None) -> None:
    decision = enforce_grounding(
        draft(
            disposition=DraftDisposition.ANSWERED,
            answer=answer,
            cited_chunk_ids=["a::decision::0::0"],
        ),
        RETRIEVED,
    )
    assert decision.accepted is False
    assert decision.violation is GroundingViolation.ANSWER_WITHOUT_TEXT


def test_an_answered_draft_carrying_a_refusal_reason_fails_closed() -> None:
    decision = enforce_grounding(
        draft(
            disposition=DraftDisposition.ANSWERED,
            answer=ANSWER,
            cited_chunk_ids=["a::decision::0::0"],
            refusal_reason=RefusalReason.OUT_OF_SCOPE,
        ),
        RETRIEVED,
    )
    assert decision.accepted is False
    assert decision.violation is GroundingViolation.ANSWER_WITH_REFUSAL_REASON


def test_a_refusal_carrying_an_answer_fails_closed() -> None:
    decision = enforce_grounding(
        draft(
            disposition=DraftDisposition.REFUSED,
            answer=ANSWER,
            refusal_reason=RefusalReason.OUT_OF_SCOPE,
        ),
        RETRIEVED,
    )
    assert decision.violation is GroundingViolation.REFUSAL_WITH_ANSWER


def test_a_refusal_carrying_citations_fails_closed() -> None:
    decision = enforce_grounding(
        draft(
            disposition=DraftDisposition.REFUSED,
            cited_chunk_ids=["a::decision::0::0"],
            refusal_reason=RefusalReason.OUT_OF_SCOPE,
        ),
        RETRIEVED,
    )
    assert decision.violation is GroundingViolation.REFUSAL_WITH_CITATIONS


def test_a_refusal_without_a_reason_fails_closed() -> None:
    decision = enforce_grounding(draft(disposition=DraftDisposition.REFUSED), RETRIEVED)
    assert decision.violation is GroundingViolation.REFUSAL_WITHOUT_REASON


def test_no_violation_ever_produces_an_answer() -> None:
    """The asymmetry that justifies failing closed."""
    hostile = [
        draft(disposition=DraftDisposition.ANSWERED, answer=ANSWER, cited_chunk_ids=["nope"]),
        draft(disposition=DraftDisposition.ANSWERED, answer=ANSWER),
        draft(
            disposition=DraftDisposition.ANSWERED, answer="", cited_chunk_ids=["a::decision::0::0"]
        ),
        draft(disposition=DraftDisposition.REFUSED),
    ]
    for candidate in hostile:
        assert enforce_grounding(candidate, RETRIEVED).accepted is False


def test_the_public_refusal_reason_does_not_reveal_which_check_failed() -> None:
    """The violation is telemetry; the caller learns only that it was ungrounded."""
    for cited in (["nope::x::0::0"], ["a::decision::0::0", "a::decision::0::0"]):
        decision = enforce_grounding(
            draft(disposition=DraftDisposition.ANSWERED, answer=ANSWER, cited_chunk_ids=cited),
            RETRIEVED,
        )
        assert decision.refusal_reason is RefusalReason.UNSUPPORTED_CITATION


def test_a_model_cannot_cite_a_document_that_was_not_retrieved() -> None:
    """Stated at the document level, which is how a reader would think about it."""
    decision = enforce_grounding(
        draft(
            disposition=DraftDisposition.ANSWERED,
            answer=ANSWER,
            cited_chunk_ids=["platform-naming-standard::rules::0::0"],
        ),
        RETRIEVED,
    )
    assert decision.accepted is False
    retrieved_docs = {r.chunk.doc_id for r in RETRIEVED}
    assert "platform-naming-standard" not in retrieved_docs
