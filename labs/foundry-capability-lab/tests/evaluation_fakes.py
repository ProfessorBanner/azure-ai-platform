"""Deterministic providers for evaluation tests. No network, no Azure, ever."""

from __future__ import annotations

from foundry_capability_lab.domain import (
    OperationalRiskAssessment,
    RiskClassification,
    Severity,
)
from foundry_capability_lab.errors import LabError
from foundry_capability_lab.evaluation.dataset import EvaluationCase
from foundry_capability_lab.evaluation.metrics import CaseOutcome
from foundry_capability_lab.telemetry import (
    InvocationResult,
    InvocationTelemetry,
    ModelMetadata,
    TokenUsage,
)


def make_case(
    case_id: str = "RISK-001",
    classification: RiskClassification = RiskClassification.SECURITY,
    severity: Severity = Severity.HIGH,
    escalate: bool = True,
    evidence: list[str] | None = None,
) -> EvaluationCase:
    return EvaluationCase(
        case_id=case_id,
        observation=f"Synthetic observation for {case_id}.",
        expected_classification=classification,
        expected_severity=severity,
        expected_requires_escalation=escalate,
        valid_evidence_ids=["EV-100"] if evidence is None else evidence,
    )


def make_result(
    classification: RiskClassification = RiskClassification.SECURITY,
    severity: Severity = Severity.HIGH,
    escalate: bool = True,
    evidence: list[str] | None = None,
    tokens: tuple[int, int, int] | None = (100, 40, 140),
) -> InvocationResult:
    usage = (
        TokenUsage()
        if tokens is None
        else TokenUsage(input_tokens=tokens[0], output_tokens=tokens[1], total_tokens=tokens[2])
    )
    return InvocationResult(
        assessment=OperationalRiskAssessment(
            risk_classification=classification,
            severity=severity,
            requires_escalation=escalate,
            rationale="Synthetic rationale that must never reach a report.",
            evidence_ids=["EV-100"] if evidence is None else evidence,
        ),
        telemetry=InvocationTelemetry(
            succeeded=True,
            latency_ms=12.0,
            model_metadata=ModelMetadata(deployment="gpt-4-1-mini", model="gpt-4.1-mini"),
            token_usage=usage,
        ),
    )


def make_outcome(
    case_id: str = "RISK-001",
    repetition: int = 1,
    *,
    schema_valid: bool = True,
    classification: RiskClassification = RiskClassification.SECURITY,
    expected_classification: RiskClassification = RiskClassification.SECURITY,
    severity: Severity = Severity.HIGH,
    expected_severity: Severity = Severity.HIGH,
    escalate: bool = True,
    expected_escalate: bool = True,
    evidence_valid: bool = True,
    latency_ms: float = 10.0,
    failure_category: object = None,
    tokens: tuple[int, int, int] | None = (100, 40, 140),
) -> CaseOutcome:
    """Build a CaseOutcome directly, for scoring tests that need no provider."""
    return CaseOutcome(
        case_id=case_id,
        repetition=repetition,
        schema_valid=schema_valid,
        latency_ms=latency_ms,
        actual_classification=classification if schema_valid else None,
        actual_severity=severity if schema_valid else None,
        actual_requires_escalation=escalate if schema_valid else None,
        evidence_valid=evidence_valid if schema_valid else None,
        expected_classification=expected_classification,
        expected_severity=expected_severity,
        expected_requires_escalation=expected_escalate,
        failure_category=failure_category,  # type: ignore[arg-type]
        input_tokens=tokens[0] if tokens and schema_valid else None,
        output_tokens=tokens[1] if tokens and schema_valid else None,
        total_tokens=tokens[2] if tokens and schema_valid else None,
    )


class ScriptedProvider:
    """Returns queued results/errors in order, and counts calls.

    The call count is what proves the runner never retries.
    """

    def __init__(self, script: list[InvocationResult | LabError]) -> None:
        self._script = list(script)
        self.calls = 0

    def assess(self, observation: str) -> InvocationResult:
        self.calls += 1
        if not self._script:
            raise AssertionError("ScriptedProvider exhausted: more calls than scripted")
        item = self._script.pop(0)
        if isinstance(item, LabError):
            raise item
        return item


class ConstantProvider:
    """Always returns the same result."""

    def __init__(self, result: InvocationResult | None = None) -> None:
        self._result = result or make_result()
        self.calls = 0

    def assess(self, observation: str) -> InvocationResult:
        self.calls += 1
        return self._result


class AlwaysFailingProvider:
    """Always raises the given error."""

    def __init__(self, error: LabError) -> None:
        self._error = error
        self.calls = 0

    def assess(self, observation: str) -> InvocationResult:
        self.calls += 1
        raise self._error
