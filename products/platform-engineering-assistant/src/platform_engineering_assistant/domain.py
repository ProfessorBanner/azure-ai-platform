"""Domain contracts.

This module is the DOMAIN BOUNDARY. It knows nothing about HTTP, Azure, Foundry,
OpenAI, BM25 or the filesystem, and it must stay that way — these are the shapes
the rest of the product agrees on, and anything provider-specific leaking in here
would have to be unpicked before a second provider could ever be used.

Every model is closed (`extra="forbid"`) and frozen. Closed matters more than it
looks: an open model silently accepts an invented field, which is precisely how
a generation provider's hallucinated key would slip through validation and be
mistaken for data.

Phase 17.1a defines these contracts but implements no API and no generation. The
invariants are enforced here, once, so that every later batch inherits them
rather than re-deriving them.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The stable chunk identifier form, documented here because the domain is where
# citations are validated even though chunking itself arrives in 17.1b:
#
#     {doc_id}::{heading-path}::{section-occurrence}::{chunk-ordinal}
#
# for example
#
#     adr-0005::decision--sandbox-is-an-environment-class::0::1
#
# Deliberately not byte offsets or a whole-file hash. Editing an early section
# must not renumber a later one, or every recorded citation breaks on an
# unrelated edit. `section-occurrence` disambiguates repeated heading paths
# (several ADRs contain more than one "## Consequences"), and `chunk-ordinal`
# orders the pieces a long section was split into.
CHUNK_ID_FORMAT = "{doc_id}::{heading_path}::{section_occurrence}::{chunk_ordinal}"

QUESTION_MIN_LENGTH = 8
QUESTION_MAX_LENGTH = 1000


class AnswerStatus(StrEnum):
    """Whether the assistant answered or declined."""

    ANSWERED = "answered"
    REFUSED = "refused"


class RefusalReason(StrEnum):
    """Why the assistant declined to answer.

    Refusal is a NORMAL outcome, not an error: "the approved corpus does not
    cover this" is a correct and useful response, and conflating it with a fault
    would teach callers to retry something that will never succeed.
    """

    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNSUPPORTED_CITATION = "unsupported_citation"
    OUT_OF_SCOPE = "out_of_scope"


class AnswerRequest(BaseModel):
    """A question to answer from the approved corpus.

    Exposes ONE field. Retrieval breadth is server-owned configuration
    (`config/retrieval_v*.json`), not a client input: letting a caller widen
    retrieval would let them tune the evidence set until an answer appeared,
    which is exactly the grounding property this product exists to protect. It
    would also make cost and latency caller-controlled.

    The question is UNTRUSTED DATA. It is never treated as instructions; see
    prompts/answer_v1.md.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str = Field(
        min_length=QUESTION_MIN_LENGTH,
        max_length=QUESTION_MAX_LENGTH,
        description="The question to answer from approved repository documentation.",
    )

    @model_validator(mode="after")
    def question_must_not_be_only_whitespace(self) -> AnswerRequest:
        if not self.question.strip():
            raise ValueError("question must contain non-whitespace characters")
        return self


class Citation(BaseModel):
    """One piece of evidence an answer rests on.

    A citation always names a CHUNK, never a whole document: "it says so in the
    ADR" is not checkable, whereas a chunk id can be verified against the set
    that was actually retrieved.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str = Field(min_length=1, description="Stable id; see CHUNK_ID_FORMAT.")
    doc_id: str = Field(min_length=1, description="Manifest id of the source document.")
    doc_path: str = Field(min_length=1, description="Repository-relative path of the document.")
    heading_path: str = Field(
        default="",
        description=(
            "Human-readable heading trail, e.g. 'Decision > Sandbox is an environment class'."
        ),
    )
    score: float = Field(ge=0.0, description="Retrieval score that admitted this chunk.")


class ModelMetadata(BaseModel):
    """Identity of the generator that produced an answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, description="Provider label, e.g. 'foundry' or 'fake'.")
    deployment: str | None = Field(default=None, description="Deployment the request addressed.")
    model: str | None = Field(default=None, description="Model id as reported by the service.")
    api_contract: str = Field(
        default="openai-responses-v1",
        description="API surface used. Informational; no call is made in Phase 17.1a.",
    )


class TokenUsage(BaseModel):
    """Token accounting, when the generator reports it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class AnswerResponse(BaseModel):
    """The answer, or a refusal, with everything needed to audit it.

    The two-way invariant below is the product's central safety property. An
    answer with no citation is NOT a weaker answer — it is ungrounded output,
    which is the failure mode this whole design exists to prevent, so the schema
    makes it unrepresentable rather than merely discouraged.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AnswerStatus
    answer: str | None = Field(default=None, description="Answer text; None when refused.")
    citations: list[Citation] = Field(
        default_factory=list,
        description="Evidence supporting the answer. Empty only when refused.",
    )
    refusal_reason: RefusalReason | None = Field(
        default=None, description="Why the assistant declined; None when answered."
    )

    request_id: str = Field(min_length=1)

    # Version stamps. An answer that cannot be attributed to a specific prompt,
    # retrieval configuration and corpus revision cannot be reproduced or
    # regression-tested, so all three travel with every response.
    prompt_version: str = Field(min_length=1)
    retrieval_config_version: str = Field(min_length=1)
    corpus_version: int = Field(ge=1)

    model_metadata: ModelMetadata
    latency_ms: float = Field(ge=0.0)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)

    @model_validator(mode="after")
    def enforce_answered_or_refused_invariant(self) -> AnswerResponse:
        if self.status is AnswerStatus.ANSWERED:
            if self.answer is None or not self.answer.strip():
                raise ValueError("an answered response must carry a non-empty answer")
            if not self.citations:
                raise ValueError("an answered response must carry at least one citation")
            if self.refusal_reason is not None:
                raise ValueError("an answered response must not carry a refusal_reason")
        else:
            if self.answer is not None:
                raise ValueError("a refused response must not carry an answer")
            if self.citations:
                raise ValueError("a refused response must not carry citations")
            if self.refusal_reason is None:
                raise ValueError("a refused response must carry a refusal_reason")
        return self
