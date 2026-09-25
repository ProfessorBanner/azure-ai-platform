"""Telemetry and result representation.

Kept separate from both the domain schema and the provider adapter so that what
is RECORDED about an invocation can evolve without touching what is asked of the
model or how the call is made.

Redaction rule for everything in this module: an instance may be printed, logged
or serialised to JSON by a caller, so no field may hold a prompt, a credential,
a bearer token, an endpoint URL or model-generated free text. Only identifiers,
counts, durations and enum values.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from foundry_capability_lab.domain import OperationalRiskAssessment
from foundry_capability_lab.errors import FailureCategory


class ModelMetadata(BaseModel):
    """Identity of the model that produced a result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deployment: str = Field(
        description="Deployment name the request was addressed to.",
    )
    model: str | None = Field(
        default=None,
        description="Model identifier as reported by the service, when returned.",
    )
    api_contract: str = Field(
        default="openai-responses-v1",
        description="API surface used for the call.",
    )


class TokenUsage(BaseModel):
    """Token accounting, when the service returns it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class InvocationTelemetry(BaseModel):
    """What the lab records about a single data-plane call.

    Populated on success AND on failure: a failed call's latency and category are
    exactly what an operator needs when diagnosing a misconfigured endpoint.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    succeeded: bool = Field(
        description="Whether the call produced a schema-valid result.",
    )
    latency_ms: float = Field(
        ge=0,
        description="Wall-clock duration of the call, measured around the request.",
    )
    model_metadata: ModelMetadata
    token_usage: TokenUsage = Field(
        default_factory=TokenUsage,
        description="Token counts when the API returns them; all None otherwise.",
    )
    request_id: str | None = Field(
        default=None,
        description=(
            "Service request/correlation id when the SDK exposes one. Useful when "
            "raising a support case; safe to log."
        ),
    )
    failure_category: FailureCategory | None = Field(
        default=None,
        description="Typed failure classification; None on success.",
    )

    def summary(self) -> str:
        """One-line, log-safe rendering.

        Contains no prompt, no credential and no response content by
        construction — every field of this model is an identifier, a count, a
        duration or an enum.
        """
        outcome = "ok" if self.succeeded else f"failed:{self.failure_category}"
        parts = [
            outcome,
            f"deployment={self.model_metadata.deployment}",
            f"latency_ms={self.latency_ms:.1f}",
        ]
        if self.token_usage.total_tokens is not None:
            parts.append(f"total_tokens={self.token_usage.total_tokens}")
        if self.request_id is not None:
            parts.append(f"request_id={self.request_id}")
        return " ".join(parts)


class InvocationResult(BaseModel):
    """A successful invocation: the validated assessment plus its telemetry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assessment: OperationalRiskAssessment
    telemetry: InvocationTelemetry
