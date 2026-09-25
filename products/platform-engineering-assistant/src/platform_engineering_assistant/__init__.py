"""Platform engineering assistant.

Grounded question answering over the APPROVED documentation of this repository's
Azure AI platform. The product answers only from an explicitly manifested corpus
and cites only evidence it was given.

Phase 17.1a establishes the foundation: domain contracts, versioned
configuration, the approved-corpus manifest, and a security-checked corpus
loader. Retrieval, generation, the HTTP API and evaluation arrive in later
batches; the contracts they will satisfy are already declared in `domain`.
"""

from platform_engineering_assistant.domain import (
    AnswerRequest,
    AnswerResponse,
    AnswerStatus,
    Citation,
    ModelMetadata,
    RefusalReason,
    TokenUsage,
)
from platform_engineering_assistant.errors import (
    AssistantError,
    ConfigurationError,
    CorpusError,
    CorpusRule,
    FailureCategory,
)

__all__ = [
    "AnswerRequest",
    "AnswerResponse",
    "AnswerStatus",
    "AssistantError",
    "Citation",
    "ConfigurationError",
    "CorpusError",
    "CorpusRule",
    "FailureCategory",
    "ModelMetadata",
    "RefusalReason",
    "TokenUsage",
]
