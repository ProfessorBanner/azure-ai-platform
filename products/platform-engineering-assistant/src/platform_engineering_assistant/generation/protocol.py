"""The provider boundary.

`GenerationProvider` is the seam. Tests substitute a deterministic fake for it,
and the Azure adapter lives entirely below it, so nothing above this line knows
that Azure, OpenAI or HTTP exist. That is what keeps the orchestration testable
without a network and what would let a second provider be added without
touching the grounding policy.

Providers return telemetry alongside the draft. They never log, and they never
decide anything: a provider's job is to turn a request into a structurally valid
draft, or to raise a typed error. Whether the draft is *acceptable* is the
grounding policy's decision, made outside the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from platform_engineering_assistant.generation.draft import GroundedDraft


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """Everything a provider needs, all of it server-owned."""

    system_prompt: str
    context: str = field(repr=False)
    question: str = field(repr=False)
    max_answer_chars: int


@dataclass(frozen=True, slots=True)
class ProviderTelemetry:
    """What a call cost and who served it. Safe to log in full."""

    provider: str
    model: str | None = None
    deployment: str | None = None
    latency_ms: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class GenerationOutcome:
    """A structurally valid draft plus the telemetry of the call that produced it."""

    draft: GroundedDraft
    telemetry: ProviderTelemetry


@runtime_checkable
class GenerationProvider(Protocol):
    """Anything that can turn a bounded request into a grounded draft."""

    @property
    def name(self) -> str:
        """Stable provider label, recorded in telemetry and in the response."""
        ...

    def generate(self, request: GenerationRequest) -> GenerationOutcome:
        """Produce one draft.

        Raises:
            AssistantError: a typed failure. Providers never raise raw transport
                or SDK exceptions past this boundary.
        """
        ...
