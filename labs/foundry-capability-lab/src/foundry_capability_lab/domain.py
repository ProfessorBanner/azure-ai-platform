"""Domain schema for the operational-risk classification example.

This module is the DOMAIN BOUNDARY. It knows nothing about Azure, Foundry,
OpenAI, HTTP or credentials, and it must stay that way: Phase 17 is expected to
reuse a schema shaped like this behind its own provider-neutral interface, and
anything Azure-specific that leaks in here would have to be unpicked first.

The schema is deliberately small. It exists to prove that a model can be made to
return validated structured output, not to model operational risk properly.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RiskClassification(StrEnum):
    """Coarse category of an operational risk observation."""

    DATA_LOSS = "data_loss"
    SERVICE_DEGRADATION = "service_degradation"
    SECURITY = "security"
    COMPLIANCE = "compliance"
    COST = "cost"
    NONE = "none"


class Severity(StrEnum):
    """Impact severity, ordered from lowest to highest."""

    INFORMATIONAL = "informational"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class OperationalRiskAssessment(BaseModel):
    """A model-produced assessment of a single operational observation.

    Every field is required and constrained. A permissive schema would let a
    malformed or partially hallucinated response validate, which would defeat
    the point of the exercise: the whole capability being proven is that invalid
    output is REJECTED rather than quietly accepted.
    """

    # extra="forbid" makes the schema closed: a model inventing an extra field
    # is a validation failure, not silently discarded data.
    model_config = ConfigDict(extra="forbid", frozen=True)

    risk_classification: RiskClassification = Field(
        description="Category of the risk identified in the observation.",
    )
    severity: Severity = Field(
        description="Impact severity of the identified risk.",
    )
    rationale: str = Field(
        min_length=1,
        max_length=1000,
        description="Short justification for the classification and severity.",
    )
    requires_escalation: bool = Field(
        description="Whether a human should be paged rather than merely notified.",
    )
    evidence_ids: list[str] = Field(
        default_factory=list,
        max_length=10,
        description=(
            "Identifiers of the evidence the assessment rests on, e.g. log or "
            "alert ids supplied in the observation. Identifiers only: the lab "
            "does not round-trip evidence content."
        ),
    )
