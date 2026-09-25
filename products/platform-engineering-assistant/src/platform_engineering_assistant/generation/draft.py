"""The contract for what a generation provider may return.

DELIBERATELY MINIMAL
--------------------
A draft carries four things: a disposition, answer text, cited chunk ids, and a
refusal reason. Nothing else.

Everything a reader would *trust* — document paths, titles, headings, line
numbers, authority levels, URLs, model metadata, request ids — is resolved
server-side from the retrieved chunks and the corpus manifest. The model is not
asked for them and could not be believed if it supplied them: a fabricated path
next to a real answer is more dangerous than a fabricated answer, because the
citation is what makes the answer credible.

So the model's entire influence over the response is: *what to say*, and *which
of the chunks it was given support it*. The second is then checked.

This model does NOT enforce the answered/refused invariant. That is deliberate:
an inconsistent draft is a policy decision to make in `grounding`, where it can
be classified and counted, not a parse error thrown here.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from platform_engineering_assistant.domain import RefusalReason

MAX_CITATIONS = 12


class DraftDisposition(StrEnum):
    """Whether the model believes it answered or declined."""

    ANSWERED = "answered"
    REFUSED = "refused"


class GroundedDraft(BaseModel):
    """Untrusted model output, structurally validated.

    Closed and frozen: an invented field is a validation failure rather than
    silently discarded data.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: DraftDisposition = Field(
        description="Whether the model answered from the supplied evidence or declined."
    )
    answer: str | None = Field(
        default=None,
        description="The answer text. Omitted when refusing.",
    )
    cited_chunk_ids: list[str] = Field(
        default_factory=list,
        max_length=MAX_CITATIONS,
        description=(
            "Chunk identifiers, copied verbatim from the supplied evidence. Every "
            "one is checked against the retrieved set; anything else fails closed."
        ),
    )
    refusal_reason: RefusalReason | None = Field(
        default=None,
        description="Why the model declined. Omitted when answering.",
    )
