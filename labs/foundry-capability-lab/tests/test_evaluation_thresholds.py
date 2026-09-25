"""Threshold loading and gating."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundry_capability_lab.errors import ConfigurationError
from foundry_capability_lab.evaluation.metrics import compute_metrics
from foundry_capability_lab.evaluation.thresholds import (
    Threshold,
    ThresholdSet,
    evaluate_gates,
    gates_passed,
    load_thresholds,
)
from tests.evaluation_fakes import make_outcome


def test_shipped_thresholds_load() -> None:
    thresholds = load_thresholds()
    assert thresholds.version >= 1
    assert thresholds.thresholds


def test_shipped_thresholds_gate_the_metrics_that_matter() -> None:
    gated = {name for name, t in load_thresholds().thresholds.items() if t.gated}
    assert {
        "schema_validity_rate",
        "classification_accuracy",
        "escalation_accuracy",
        "evidence_validity_rate",
        "repeated_call_consistency",
        "provider_error_rate",
    } <= gated


def test_latency_and_severity_are_not_gated() -> None:
    """Both are observational; gating them would fail runs for the wrong reason."""
    thresholds = load_thresholds().thresholds
    assert thresholds["latency_p95_ms"].gated is False
    assert thresholds["severity_accuracy"].gated is False


def test_every_shipped_threshold_names_a_real_metric() -> None:
    metrics = compute_metrics([make_outcome()])
    # evaluate_gates raises if a threshold names an unknown metric.
    results = evaluate_gates(metrics, load_thresholds())
    assert len(results) == len(load_thresholds().thresholds)


def test_documentation_keys_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text(
        json.dumps(
            {
                "$comment": ["notes for humans"],
                "version": 2,
                "thresholds": {"schema_validity_rate": {"gated": True, "minimum": 1.0}},
            }
        )
    )
    assert load_thresholds(path).version == 2


def test_missing_file_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        load_thresholds(Path("/nonexistent/t.json"))


def test_malformed_json_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text("{not json")
    with pytest.raises(ConfigurationError):
        load_thresholds(path)


def test_threshold_without_a_bound_is_rejected(tmp_path: Path) -> None:
    """A bound-less threshold can never fail, so it is a silent non-gate."""
    path = tmp_path / "t.json"
    path.write_text(
        json.dumps({"version": 1, "thresholds": {"classification_accuracy": {"gated": True}}})
    )
    with pytest.raises(ConfigurationError) as caught:
        load_thresholds(path)
    assert "classification_accuracy" in str(caught.value)


def test_threshold_naming_an_unknown_metric_is_rejected() -> None:
    """A typo must not become an unenforced gate."""
    thresholds = ThresholdSet(
        version=1, thresholds={"not_a_metric": Threshold(gated=True, minimum=0.5)}
    )
    with pytest.raises(ConfigurationError) as caught:
        evaluate_gates(compute_metrics([make_outcome()]), thresholds)
    assert "not_a_metric" in str(caught.value)


@pytest.mark.parametrize(
    ("threshold", "value", "expected"),
    [
        (Threshold(gated=True, minimum=0.75), 0.75, True),
        (Threshold(gated=True, minimum=0.75), 0.7499, False),
        (Threshold(gated=True, maximum=0.1), 0.1, True),
        (Threshold(gated=True, maximum=0.1), 0.11, False),
    ],
)
def test_bounds_are_inclusive(threshold: Threshold, value: float, expected: bool) -> None:
    assert threshold.check(value) is expected


def test_ungated_failure_does_not_fail_the_run() -> None:
    metrics = compute_metrics([make_outcome()])
    thresholds = ThresholdSet(
        version=1,
        thresholds={
            "classification_accuracy": Threshold(gated=True, minimum=0.5),
            "latency_p95_ms": Threshold(gated=False, maximum=0.0001),
        },
    )
    results = evaluate_gates(metrics, thresholds)
    assert any(not r.passed for r in results)
    assert gates_passed(results) is True


def test_gated_failure_fails_the_run() -> None:
    metrics = compute_metrics([make_outcome(evidence_valid=False)])
    thresholds = ThresholdSet(
        version=1, thresholds={"evidence_validity_rate": Threshold(gated=True, minimum=0.9)}
    )
    assert gates_passed(evaluate_gates(metrics, thresholds)) is False
