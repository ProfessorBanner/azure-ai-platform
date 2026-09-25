"""Domain schema: valid results accepted, invalid results rejected.

Schema rejection is the capability under test. If a partially hallucinated or
malformed response validated, the structured-output proof would be worthless.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry_capability_lab.domain import (
    OperationalRiskAssessment,
    RiskClassification,
    Severity,
)
from tests.fakes import sample_assessment


def test_valid_assessment_is_accepted() -> None:
    assessment = sample_assessment()
    assert assessment.risk_classification is RiskClassification.SERVICE_DEGRADATION
    assert assessment.severity is Severity.MEDIUM
    assert assessment.requires_escalation is False
    assert assessment.evidence_ids == ["ALERT-4471", "INC-2208"]


def test_assessment_is_immutable() -> None:
    assessment = sample_assessment()
    with pytest.raises(ValidationError):
        assessment.severity = Severity.CRITICAL


def test_evidence_ids_default_to_empty() -> None:
    assessment = OperationalRiskAssessment(
        risk_classification=RiskClassification.NONE,
        severity=Severity.INFORMATIONAL,
        rationale="Nothing of concern.",
        requires_escalation=False,
    )
    assert assessment.evidence_ids == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("risk_classification", "catastrophe"),  # not a member of the enum
        ("severity", "apocalyptic"),
        ("rationale", ""),  # min_length=1
        ("requires_escalation", "probably"),  # not coercible to bool
    ],
)
def test_invalid_field_values_are_rejected(field: str, value: object) -> None:
    payload: dict[str, object] = {
        "risk_classification": "security",
        "severity": "high",
        "rationale": "Credentials were exposed in a log.",
        "requires_escalation": True,
    }
    payload[field] = value
    with pytest.raises(ValidationError):
        OperationalRiskAssessment.model_validate(payload)


def test_missing_required_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        OperationalRiskAssessment.model_validate(
            {
                "risk_classification": "cost",
                "severity": "low",
                # rationale omitted
                "requires_escalation": False,
            }
        )


def test_unknown_field_is_rejected() -> None:
    """A closed schema: an invented field is a failure, not silently dropped."""
    with pytest.raises(ValidationError):
        OperationalRiskAssessment.model_validate(
            {
                "risk_classification": "cost",
                "severity": "low",
                "rationale": "Spend rose.",
                "requires_escalation": False,
                "confidence": 0.91,  # not in the schema
            }
        )


def test_too_many_evidence_ids_are_rejected() -> None:
    with pytest.raises(ValidationError):
        OperationalRiskAssessment.model_validate(
            {
                "risk_classification": "cost",
                "severity": "low",
                "rationale": "Spend rose.",
                "requires_escalation": False,
                "evidence_ids": [f"EV-{index}" for index in range(11)],
            }
        )
