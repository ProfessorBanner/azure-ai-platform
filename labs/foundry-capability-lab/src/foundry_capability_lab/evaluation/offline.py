"""A deterministic, offline provider used for rehearsing the harness.

WHY THIS EXISTS
---------------
The evaluation pipeline has to be exercisable without Azure: on a machine with
no `az login`, without the `Foundry User` role, and off the account's IP
allow-list. Without that, the harness itself could only ever be debugged during
a live run, which is the worst possible time.

WHAT IT IS NOT
--------------
It is NOT a model, and its scores are NOT a capability result. It applies fixed
keyword rules to the observation text, so a run against it measures whether the
plumbing works — dataset loads, metrics compute, gates evaluate, reports write —
and nothing whatsoever about gpt-4-1-mini. Reports label the provider so an
offline run can never be mistaken for a real one.
"""

from __future__ import annotations

import hashlib
import re

from foundry_capability_lab.domain import (
    OperationalRiskAssessment,
    RiskClassification,
    Severity,
)
from foundry_capability_lab.telemetry import (
    InvocationResult,
    InvocationTelemetry,
    ModelMetadata,
    TokenUsage,
)

PROVIDER_LABEL = "offline-deterministic"

# Ordered most- to least-specific; the first match wins.
_CLASSIFICATION_RULES: tuple[tuple[RiskClassification, tuple[str, ...]], ...] = (
    (RiskClassification.SECURITY, ("access key", "plaintext", "owner on the subscription")),
    (RiskClassification.COMPLIANCE, ("gdpr", "data handling standard", "bypassing")),
    (RiskClassification.DATA_LOSS, ("purged", "zero-byte", "retention policy")),
    (RiskClassification.COST, ("spend", "budget", "gbp")),
    (RiskClassification.SERVICE_DEGRADATION, ("failed", "latency", "down for", "alert")),
)

_SEVERITY_RULES: tuple[tuple[Severity, tuple[str, ...]], ...] = (
    (Severity.CRITICAL, ("still active", "has been down", "200 times")),
    (Severity.HIGH, ("already been purged", "no access restriction", "holds owner")),
    (Severity.LOW, ("has not been breached",)),
    (Severity.INFORMATIONAL, ("completed successfully",)),
)

_ESCALATION_MARKERS = (
    "still active",
    "already been purged",
    "no access restriction",
    "has been down",
    "holds owner",
    "200 times",
)

_EVIDENCE_PATTERN = re.compile(r"\b[A-Z]{3,4}-\d{3,4}\b")


class OfflineRiskAssessmentProvider:
    """Rule-based stand-in satisfying `RiskAssessmentProvider`."""

    def __init__(self, deployment: str = PROVIDER_LABEL) -> None:
        self._deployment = deployment

    def assess(self, observation: str) -> InvocationResult:
        lowered = observation.lower()

        classification = RiskClassification.NONE
        for candidate, markers in _CLASSIFICATION_RULES:
            if any(marker in lowered for marker in markers):
                classification = candidate
                break

        severity = Severity.MEDIUM
        for severity_candidate, severity_markers in _SEVERITY_RULES:
            if any(marker in lowered for marker in severity_markers):
                severity = severity_candidate
                break

        escalate = any(marker in lowered for marker in _ESCALATION_MARKERS)

        # Evidence identifiers are extracted verbatim, so a well-behaved offline
        # run never fabricates one.
        evidence_ids = list(dict.fromkeys(_EVIDENCE_PATTERN.findall(observation)))

        assessment = OperationalRiskAssessment(
            risk_classification=classification,
            severity=severity,
            requires_escalation=escalate,
            rationale="Offline deterministic rule match; not model output.",
            evidence_ids=evidence_ids[:10],
        )

        # Stable pseudo-telemetry derived from the input, so repeated offline
        # runs produce byte-identical reports apart from the timestamp.
        digest = hashlib.sha256(observation.encode()).digest()
        input_tokens = 80 + digest[0] % 60
        output_tokens = 30 + digest[1] % 20

        return InvocationResult(
            assessment=assessment,
            telemetry=InvocationTelemetry(
                succeeded=True,
                latency_ms=float(20 + digest[2] % 80),
                model_metadata=ModelMetadata(deployment=self._deployment, model=PROVIDER_LABEL),
                token_usage=TokenUsage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                ),
                request_id=None,
            ),
        )
