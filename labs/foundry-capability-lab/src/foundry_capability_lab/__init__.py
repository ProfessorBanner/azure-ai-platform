"""Phase 16.2 Foundry data-plane capability proof.

This package proves that the sandbox Microsoft Foundry deployment can be invoked
keylessly with a Microsoft Entra ID token and made to return a strictly typed,
schema-validated result, with latency and token usage captured.

It is a LAB, not the Phase 17 product. See README.md.
"""

from foundry_capability_lab.domain import (
    OperationalRiskAssessment,
    RiskClassification,
    Severity,
)
from foundry_capability_lab.errors import (
    AuthenticationError,
    AuthorizationError,
    FailureCategory,
    InvalidStructuredOutputError,
    LabError,
    NetworkDeniedError,
    ProviderError,
    RateLimitedError,
    TimeoutError_,
)
from foundry_capability_lab.telemetry import InvocationResult, InvocationTelemetry, ModelMetadata

__all__ = [
    "AuthenticationError",
    "AuthorizationError",
    "FailureCategory",
    "InvalidStructuredOutputError",
    "InvocationResult",
    "InvocationTelemetry",
    "LabError",
    "ModelMetadata",
    "NetworkDeniedError",
    "OperationalRiskAssessment",
    "ProviderError",
    "RateLimitedError",
    "RiskClassification",
    "Severity",
    "TimeoutError_",
]
