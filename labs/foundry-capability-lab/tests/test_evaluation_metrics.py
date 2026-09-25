"""Scoring: every metric, and the denominators it is a rate of."""

from __future__ import annotations

import pytest

from foundry_capability_lab.domain import RiskClassification, Severity
from foundry_capability_lab.errors import FailureCategory
from foundry_capability_lab.evaluation.metrics import compute_metrics, percentile
from tests.evaluation_fakes import make_outcome


def test_perfect_run_scores_one_across_the_board() -> None:
    outcomes = [
        make_outcome(case_id=f"C-{index}", repetition=rep) for index in range(4) for rep in (1, 2)
    ]
    metrics = compute_metrics(outcomes)

    assert metrics.attempts == 8
    assert metrics.valid_outputs == 8
    assert metrics.cases == 4
    assert metrics.repetitions == 2
    assert metrics.schema_validity_rate == 1.0
    assert metrics.classification_accuracy == 1.0
    assert metrics.escalation_accuracy == 1.0
    assert metrics.evidence_validity_rate == 1.0
    assert metrics.repeated_call_consistency == 1.0
    assert metrics.provider_error_rate == 0.0


def test_accuracy_denominator_is_valid_outputs_not_attempts() -> None:
    """A failed call has no classification to be wrong about.

    Two attempts, one failure, one correct answer: accuracy is 1/1, not 1/2.
    Dividing by attempts would conflate 'unreachable' with 'incorrect'.
    """
    outcomes = [
        make_outcome(case_id="A", repetition=1),
        make_outcome(
            case_id="A",
            repetition=2,
            schema_valid=False,
            failure_category=FailureCategory.PROVIDER_ERROR,
        ),
    ]
    metrics = compute_metrics(outcomes)

    assert metrics.attempts == 2
    assert metrics.valid_outputs == 1
    assert metrics.schema_validity_rate == 0.5
    assert metrics.provider_error_rate == 0.5
    assert metrics.classification_accuracy == 1.0


def test_classification_and_escalation_scored_independently() -> None:
    outcomes = [
        make_outcome(
            classification=RiskClassification.COST,
            expected_classification=RiskClassification.SECURITY,
            escalate=True,
            expected_escalate=True,
        )
    ]
    metrics = compute_metrics(outcomes)
    assert metrics.classification_accuracy == 0.0
    assert metrics.escalation_accuracy == 1.0


def test_severity_accuracy_is_computed_separately() -> None:
    outcomes = [
        make_outcome(severity=Severity.LOW, expected_severity=Severity.CRITICAL),
        make_outcome(case_id="B", severity=Severity.HIGH, expected_severity=Severity.HIGH),
    ]
    assert compute_metrics(outcomes).severity_accuracy == 0.5


def test_evidence_validity_rate() -> None:
    outcomes = [
        make_outcome(case_id="A", evidence_valid=True),
        make_outcome(case_id="B", evidence_valid=False),
        make_outcome(case_id="C", evidence_valid=True),
        make_outcome(case_id="D", evidence_valid=True),
    ]
    assert compute_metrics(outcomes).evidence_validity_rate == 0.75


# --- consistency ------------------------------------------------------------


def test_consistency_requires_every_repetition_to_agree() -> None:
    agreeing = [make_outcome(case_id="A", repetition=rep) for rep in (1, 2, 3)]
    disagreeing = [
        make_outcome(case_id="B", repetition=1, classification=RiskClassification.COST),
        make_outcome(case_id="B", repetition=2, classification=RiskClassification.SECURITY),
        make_outcome(case_id="B", repetition=3, classification=RiskClassification.COST),
    ]
    assert compute_metrics(agreeing + disagreeing).repeated_call_consistency == 0.5


def test_consistency_counts_severity_and_escalation_too() -> None:
    outcomes = [
        make_outcome(case_id="A", repetition=1, severity=Severity.HIGH),
        make_outcome(case_id="A", repetition=2, severity=Severity.LOW),
    ]
    assert compute_metrics(outcomes).repeated_call_consistency == 0.0


def test_a_case_that_failed_once_is_not_consistent() -> None:
    """Flakiness must not be rewarded as agreement."""
    outcomes = [
        make_outcome(case_id="A", repetition=1),
        make_outcome(
            case_id="A",
            repetition=2,
            schema_valid=False,
            failure_category=FailureCategory.TIMEOUT,
        ),
    ]
    assert compute_metrics(outcomes).repeated_call_consistency == 0.0


def test_single_repetition_cases_are_excluded_from_consistency() -> None:
    """One sample says nothing about repeatability, so it must not score 100%."""
    metrics = compute_metrics([make_outcome(case_id="A", repetition=1)])
    assert metrics.repeated_call_consistency == 0.0


# --- telemetry --------------------------------------------------------------


def test_latency_percentiles_use_nearest_rank() -> None:
    outcomes = [
        make_outcome(case_id=f"C-{index}", latency_ms=float(value))
        for index, value in enumerate([10, 20, 30, 40, 100])
    ]
    metrics = compute_metrics(outcomes)
    assert metrics.latency_p50_ms == 30.0
    assert metrics.latency_p95_ms == 100.0


def test_latency_includes_failed_attempts() -> None:
    """p95 must describe every call made, not only the flattering ones."""
    outcomes = [
        make_outcome(case_id="A", latency_ms=10.0),
        make_outcome(
            case_id="B",
            latency_ms=900.0,
            schema_valid=False,
            failure_category=FailureCategory.TIMEOUT,
        ),
    ]
    assert compute_metrics(outcomes).latency_p95_ms == 900.0


def test_tokens_are_summed_and_absent_counts_ignored() -> None:
    outcomes = [
        make_outcome(case_id="A", tokens=(100, 40, 140)),
        make_outcome(case_id="B", tokens=None),
    ]
    metrics = compute_metrics(outcomes)
    assert metrics.input_tokens == 100
    assert metrics.output_tokens == 40
    assert metrics.total_tokens == 140


def test_failure_categories_are_counted() -> None:
    outcomes = [
        make_outcome(
            case_id="A", schema_valid=False, failure_category=FailureCategory.RATE_LIMITED
        ),
        make_outcome(
            case_id="B", schema_valid=False, failure_category=FailureCategory.RATE_LIMITED
        ),
        make_outcome(case_id="C", schema_valid=False, failure_category=FailureCategory.TIMEOUT),
    ]
    assert compute_metrics(outcomes).failure_counts == {"rate_limited": 2, "timeout": 1}


def test_empty_run_scores_zero_rather_than_dividing_by_zero() -> None:
    """An empty run must never read as a pass."""
    metrics = compute_metrics([])
    assert metrics.attempts == 0
    assert metrics.schema_validity_rate == 0.0
    assert metrics.classification_accuracy == 0.0
    assert metrics.latency_p50_ms == 0.0


def test_scoring_is_deterministic() -> None:
    outcomes = [make_outcome(case_id=f"C-{i}", repetition=r) for i in range(3) for r in (1, 2)]
    assert compute_metrics(outcomes) == compute_metrics(list(reversed(outcomes)))


@pytest.mark.parametrize(
    ("values", "fraction", "expected"),
    [
        ([], 0.5, 0.0),
        ([5.0], 0.5, 5.0),
        ([1.0, 2.0, 3.0, 4.0], 0.5, 2.0),
        ([1.0, 2.0, 3.0, 4.0], 1.0, 4.0),
    ],
)
def test_percentile_cases(values: list[float], fraction: float, expected: float) -> None:
    assert percentile(values, fraction) == expected


def test_percentile_rejects_an_out_of_range_fraction() -> None:
    with pytest.raises(ValueError):
        percentile([1.0], 1.5)
