"""Domain contracts and their invariants."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from platform_engineering_assistant.domain import (
    CHUNK_ID_FORMAT,
    AnswerRequest,
    AnswerResponse,
    AnswerStatus,
    Citation,
    ModelMetadata,
    RefusalReason,
    TokenUsage,
)

CITATION = Citation(
    chunk_id="adr-0005::decision::0::1",
    doc_id="adr-0005",
    doc_path="docs/adr/0005-four-environment-databricks-foundation.md",
    heading_path="Decision",
    score=4.2,
)

BASE: dict[str, Any] = {
    "request_id": "req-1",
    "prompt_version": "answer_v1",
    "retrieval_config_version": "retrieval_v1",
    "corpus_version": 1,
    "model_metadata": ModelMetadata(provider="fake"),
    "latency_ms": 12.0,
}


def build_response(**overrides: Any) -> AnswerResponse:
    """Construct a response from BASE plus overrides.

    Goes through `model_validate` rather than keyword arguments so a
    deliberately heterogeneous fixture dict does not fight strict typing. The
    validators under test run either way.
    """
    return AnswerResponse.model_validate({**BASE, **overrides})


# --- AnswerRequest ----------------------------------------------------------


def test_question_of_valid_length_is_accepted() -> None:
    assert AnswerRequest(question="How is Terraform state stored?").question


@pytest.mark.parametrize("question", ["short", "", "   a   "])
def test_question_below_minimum_length_is_rejected(question: str) -> None:
    with pytest.raises(ValidationError):
        AnswerRequest(question=question)


def test_question_above_maximum_length_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AnswerRequest(question="q" * 1001)


def test_whitespace_only_question_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AnswerRequest(question=" " * 50)


def test_request_exposes_only_the_question() -> None:
    """Retrieval breadth is server-owned; a caller must not be able to widen it."""
    assert set(AnswerRequest.model_fields) == {"question"}


def test_max_chunks_is_rejected_as_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        AnswerRequest.model_validate({"question": "How is state stored?", "max_chunks": 50})


def test_request_is_immutable() -> None:
    request = AnswerRequest(question="How is Terraform state stored?")
    with pytest.raises(ValidationError):
        request.question = "something else"


# --- AnswerResponse invariants ----------------------------------------------


def test_answered_response_with_answer_and_citation_is_valid() -> None:
    response = build_response(
        status=AnswerStatus.ANSWERED,
        answer="State lives in Azure Blob Storage.",
        citations=[CITATION],
    )
    assert response.status is AnswerStatus.ANSWERED


def test_answered_response_without_citations_is_rejected() -> None:
    """An answer with no citation is ungrounded output, not a weaker answer."""
    with pytest.raises(ValidationError):
        build_response(status=AnswerStatus.ANSWERED, answer="Some claim.", citations=[])


@pytest.mark.parametrize("answer", [None, "", "   "])
def test_answered_response_without_an_answer_is_rejected(answer: str | None) -> None:
    with pytest.raises(ValidationError):
        build_response(status=AnswerStatus.ANSWERED, answer=answer, citations=[CITATION])


def test_answered_response_carrying_a_refusal_reason_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_response(
            status=AnswerStatus.ANSWERED,
            answer="A claim.",
            citations=[CITATION],
            refusal_reason=RefusalReason.OUT_OF_SCOPE,
        )


def test_refused_response_is_valid() -> None:
    response = build_response(
        status=AnswerStatus.REFUSED, refusal_reason=RefusalReason.INSUFFICIENT_EVIDENCE
    )
    assert response.answer is None
    assert response.citations == []


def test_refused_response_without_a_reason_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_response(status=AnswerStatus.REFUSED)


def test_refused_response_carrying_an_answer_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_response(
            status=AnswerStatus.REFUSED,
            answer="A sneaky answer.",
            refusal_reason=RefusalReason.OUT_OF_SCOPE,
        )


def test_refused_response_carrying_citations_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_response(
            status=AnswerStatus.REFUSED,
            citations=[CITATION],
            refusal_reason=RefusalReason.OUT_OF_SCOPE,
        )


def test_response_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        build_response(
            status=AnswerStatus.REFUSED, refusal_reason=RefusalReason.OUT_OF_SCOPE, confidence=0.9
        )


def test_response_requires_all_three_version_stamps() -> None:
    for omitted in ("prompt_version", "retrieval_config_version", "corpus_version"):
        payload = {key: value for key, value in BASE.items() if key != omitted}
        with pytest.raises(ValidationError):
            AnswerResponse.model_validate(
                {
                    **payload,
                    "status": AnswerStatus.REFUSED,
                    "refusal_reason": RefusalReason.OUT_OF_SCOPE,
                }
            )


# --- supporting models ------------------------------------------------------


def test_citation_rejects_a_negative_score() -> None:
    with pytest.raises(ValidationError):
        Citation(chunk_id="c", doc_id="d", doc_path="p", score=-1.0)


def test_token_usage_defaults_to_all_none() -> None:
    usage = TokenUsage()
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (None, None, None)


def test_chunk_id_format_documents_all_four_components() -> None:
    """The form is fixed in 17.1a so citations recorded later stay stable."""
    for component in ("doc_id", "heading_path", "section_occurrence", "chunk_ordinal"):
        assert f"{{{component}}}" in CHUNK_ID_FORMAT
