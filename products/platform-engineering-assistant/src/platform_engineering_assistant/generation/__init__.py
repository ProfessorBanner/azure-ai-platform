"""Grounded generation: the provider boundary and its implementations."""

from platform_engineering_assistant.generation.draft import DraftDisposition, GroundedDraft
from platform_engineering_assistant.generation.fake import (
    FakeBehaviour,
    FakeGenerationProvider,
)
from platform_engineering_assistant.generation.protocol import (
    GenerationOutcome,
    GenerationProvider,
    GenerationRequest,
    ProviderTelemetry,
)

__all__ = [
    "DraftDisposition",
    "FakeBehaviour",
    "FakeGenerationProvider",
    "GenerationOutcome",
    "GenerationProvider",
    "GenerationRequest",
    "GroundedDraft",
    "ProviderTelemetry",
]
