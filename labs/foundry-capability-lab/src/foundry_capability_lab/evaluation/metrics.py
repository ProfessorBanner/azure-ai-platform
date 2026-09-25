"""Deterministic scoring.

Every function here is pure: given the same `CaseOutcome` list it returns the
same metrics, forever. That is what makes the harness trustworthy even though
the model underneath is not deterministic.

DENOMINATORS ARE EXPLICIT AND FIXED
-----------------------------------
Each (case, repetition) pair is attempted exactly ONCE. There is no retry
anywhere in this package. A retry would quietly change what a rate is a rate
*of* — re-running failures until they pass would make an error rate measure
persistence rather than reliability. Two denominators are therefore reported
alongside the metrics themselves:

  attempts        every call made (successes and failures alike)
  valid_outputs   attempts that returned a schema-valid assessment

Accuracy metrics divide by `valid_outputs`, because a response that never
validated has no classification to be right or wrong about. Rate metrics
(schema validity, provider errors) divide by `attempts`.
"""

from __future__ import annotations

import math
from collections import defaultdict

from pydantic import BaseModel, ConfigDict, Field

from foundry_capability_lab.domain import RiskClassification, Severity
from foundry_capability_lab.errors import FailureCategory


class CaseOutcome(BaseModel):
    """The recorded result of ONE attempt at ONE case.

    Contains no observation text and no model rationale — only identifiers,
    decisions and measurements, so a list of these is safe to serialise.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    repetition: int = Field(ge=1)
    schema_valid: bool
    latency_ms: float = Field(ge=0)

    # Populated only when schema_valid is True.
    actual_classification: RiskClassification | None = None
    actual_severity: Severity | None = None
    actual_requires_escalation: bool | None = None
    evidence_valid: bool | None = None
    fabricated_evidence_count: int = Field(default=0, ge=0)

    # Ground truth, carried so scoring needs no second lookup.
    expected_classification: RiskClassification
    expected_severity: Severity
    expected_requires_escalation: bool

    # Populated only when the attempt failed.
    failure_category: FailureCategory | None = None

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    @property
    def classification_correct(self) -> bool:
        return self.schema_valid and self.actual_classification == self.expected_classification

    @property
    def severity_correct(self) -> bool:
        return self.schema_valid and self.actual_severity == self.expected_severity

    @property
    def escalation_correct(self) -> bool:
        return (
            self.schema_valid
            and self.actual_requires_escalation == self.expected_requires_escalation
        )


class EvaluationMetrics(BaseModel):
    """Aggregate scores plus the denominators they were computed over."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempts: int = Field(ge=0)
    valid_outputs: int = Field(ge=0)
    cases: int = Field(ge=0)
    repetitions: int = Field(ge=0)

    schema_validity_rate: float
    classification_accuracy: float
    escalation_accuracy: float
    severity_accuracy: float
    evidence_validity_rate: float
    repeated_call_consistency: float
    provider_error_rate: float

    latency_p50_ms: float
    latency_p95_ms: float

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    failure_counts: dict[str, int] = Field(default_factory=dict)
    fabricated_evidence_count: int = Field(default=0, ge=0)


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile.

    Chosen over interpolation deliberately: at these sample sizes (12 cases x 3
    repetitions) an interpolated p95 reports a latency that no call actually
    had. Nearest-rank always returns an observed measurement.
    """
    if not values:
        return 0.0
    if not 0.0 < fraction <= 1.0:
        raise ValueError("fraction must be in (0, 1]")
    ordered = sorted(values)
    rank = math.ceil(fraction * len(ordered))
    return ordered[max(1, rank) - 1]


def _ratio(numerator: int, denominator: int) -> float:
    """Rates over an empty denominator are 0.0, never a division error.

    0.0 is the safe direction: an empty run must not read as a pass.
    """
    return numerator / denominator if denominator else 0.0


def compute_metrics(outcomes: list[CaseOutcome]) -> EvaluationMetrics:
    """Score a completed run. Pure; performs no I/O."""
    attempts = len(outcomes)
    valid = [outcome for outcome in outcomes if outcome.schema_valid]
    valid_outputs = len(valid)

    case_ids = {outcome.case_id for outcome in outcomes}
    repetition_numbers = {outcome.repetition for outcome in outcomes}

    provider_errors = sum(1 for outcome in outcomes if not outcome.schema_valid)

    failure_counts: dict[str, int] = defaultdict(int)
    for outcome in outcomes:
        if outcome.failure_category is not None:
            failure_counts[str(outcome.failure_category)] += 1

    latencies = [outcome.latency_ms for outcome in outcomes]

    return EvaluationMetrics(
        attempts=attempts,
        valid_outputs=valid_outputs,
        cases=len(case_ids),
        repetitions=len(repetition_numbers),
        schema_validity_rate=_ratio(valid_outputs, attempts),
        classification_accuracy=_ratio(
            sum(1 for outcome in valid if outcome.classification_correct), valid_outputs
        ),
        escalation_accuracy=_ratio(
            sum(1 for outcome in valid if outcome.escalation_correct), valid_outputs
        ),
        severity_accuracy=_ratio(
            sum(1 for outcome in valid if outcome.severity_correct), valid_outputs
        ),
        evidence_validity_rate=_ratio(
            sum(1 for outcome in valid if outcome.evidence_valid), valid_outputs
        ),
        repeated_call_consistency=_consistency(outcomes),
        provider_error_rate=_ratio(provider_errors, attempts),
        latency_p50_ms=percentile(latencies, 0.50),
        latency_p95_ms=percentile(latencies, 0.95),
        input_tokens=sum(outcome.input_tokens or 0 for outcome in outcomes),
        output_tokens=sum(outcome.output_tokens or 0 for outcome in outcomes),
        total_tokens=sum(outcome.total_tokens or 0 for outcome in outcomes),
        failure_counts=dict(sorted(failure_counts.items())),
        fabricated_evidence_count=sum(outcome.fabricated_evidence_count for outcome in outcomes),
    )


def _consistency(outcomes: list[CaseOutcome]) -> float:
    """Fraction of cases whose repetitions all produced the same decision.

    The decision signature is (classification, severity, escalation). A case is
    consistent only if every one of its attempts is schema-valid AND they all
    agree — a case that failed on one repetition is not "consistent", it is
    unreliable, and counting it as consistent would reward flakiness.

    Cases attempted only once are excluded from the denominator entirely:
    a single sample carries no information about repeatability, and scoring it
    as perfectly consistent would let `--repetitions 1` inflate the metric.
    """
    grouped: dict[str, list[CaseOutcome]] = defaultdict(list)
    for outcome in outcomes:
        grouped[outcome.case_id].append(outcome)

    repeated = {case_id: attempts for case_id, attempts in grouped.items() if len(attempts) > 1}
    if not repeated:
        return 0.0

    consistent = 0
    for attempts in repeated.values():
        if not all(attempt.schema_valid for attempt in attempts):
            continue
        signatures = {
            (
                attempt.actual_classification,
                attempt.actual_severity,
                attempt.actual_requires_escalation,
            )
            for attempt in attempts
        }
        if len(signatures) == 1:
            consistent += 1

    return _ratio(consistent, len(repeated))
