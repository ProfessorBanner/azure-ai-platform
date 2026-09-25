"""Safe operational telemetry.

REDACTION IS STRUCTURAL
-----------------------
This record is designed so that it CANNOT carry sensitive content: there is no
field for the question, the context, the answer, a chunk body, a token or a
credential. Redaction is therefore not a step someone has to remember — the
fields simply do not exist, and a closed model rejects an attempt to add one.

What it does carry: identifiers, durations, counts, versions and enum values.

The one judgement call worth stating plainly: `question_chars` and
`retrieved_chunk_count` are recorded, and `answered` reveals whether the corpus
covered a question. That is a small amount of signal about what was asked, and
it is the price of being able to diagnose a bad day at all. It is not the
question text, and no field here can be reassembled into it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from platform_engineering_assistant.domain import AnswerStatus, RefusalReason
from platform_engineering_assistant.errors import FailureCategory
from platform_engineering_assistant.grounding import GroundingViolation


@dataclass(frozen=True, slots=True)
class RequestTelemetry:
    """One request, described without repeating any of its content."""

    request_id: str

    retrieval_ms: float = 0.0
    generation_ms: float = 0.0
    total_ms: float = 0.0

    retrieved_chunk_count: int = 0
    context_chars: int = 0
    context_chunks_dropped: int = 0
    question_chars: int = 0

    prompt_version: str = ""
    prompt_hash: str = ""
    retrieval_config_version: str = ""
    generation_config_version: str = ""
    corpus_version: int = 0

    provider: str = ""
    model: str | None = None
    deployment: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    provider_request_id: str | None = None

    status: AnswerStatus | None = None
    citation_count: int = 0
    refusal_reason: RefusalReason | None = None
    grounding_violation: GroundingViolation | None = None
    failure_category: FailureCategory | None = None

    # Retrieved chunk ids. Public repository document identifiers, useful for
    # reproducing a bad answer; kept optional so a deployment can drop them.
    retrieved_chunk_ids: tuple[str, ...] = field(default=())

    def summary(self) -> str:
        """One-line, log-safe rendering."""
        outcome = self.status.value if self.status else f"error:{self.failure_category}"
        parts = [
            f"request_id={self.request_id}",
            f"outcome={outcome}",
            f"total_ms={self.total_ms:.1f}",
            f"retrieval_ms={self.retrieval_ms:.1f}",
            f"generation_ms={self.generation_ms:.1f}",
            f"chunks={self.retrieved_chunk_count}",
            f"citations={self.citation_count}",
            f"prompt={self.prompt_version}@{self.prompt_hash[:12]}",
            f"provider={self.provider}",
        ]
        if self.total_tokens is not None:
            parts.append(f"total_tokens={self.total_tokens}")
        if self.refusal_reason is not None:
            parts.append(f"refusal={self.refusal_reason}")
        if self.grounding_violation is not None:
            parts.append(f"violation={self.grounding_violation}")
        return " ".join(parts)

    def as_dict(self) -> dict[str, object]:
        """Serialisable form for a structured log sink."""
        return asdict(self)
