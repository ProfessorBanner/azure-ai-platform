"""The gate: thresholds, modes, and the rule that a missing metric never passes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from platform_engineering_assistant.errors import ConfigurationError
from platform_engineering_assistant.evaluation.policy import (
    DEFAULT_POLICY_PATH,
    EvaluationPolicy,
    ExecutionMode,
    GateStatus,
    Threshold,
    evaluate_gates,
    load_policy,
    overall_status,
)


@pytest.fixture(scope="module")
def policy() -> EvaluationPolicy:
    return load_policy()


# --- the shipped policy -----------------------------------------------------


@pytest.mark.parametrize(
    ("metric", "bound", "value"),
    [
        ("schema_validity_rate", "minimum", 1.0),
        ("citation_containment_rate", "minimum", 1.0),
        ("prohibited_content_rate", "maximum", 0.0),
        ("provider_failure_rate", "maximum", 0.0),
        ("disposition_accuracy", "minimum", 0.875),
        ("refusal_accuracy", "minimum", 0.83),
        ("expected_document_citation_recall", "minimum", 0.75),
        ("semantic_groundedness", "minimum", 0.85),
        ("unsupported_claim_rate", "maximum", 0.15),
        ("latency_p95_ms", "maximum", 15000.0),
        ("total_tokens_p95", "maximum", 4000.0),
    ],
)
def test_the_shipped_thresholds_are_the_agreed_values(
    policy: EvaluationPolicy, metric: str, bound: str, value: float
) -> None:
    assert getattr(policy.thresholds[metric], bound) == value


def test_every_threshold_records_a_rationale(policy: EvaluationPolicy) -> None:
    """A number with no recorded reason cannot be recalibrated, only re-guessed."""
    for name, threshold in policy.thresholds.items():
        assert len(threshold.rationale) > 40, name


def test_the_judge_gates_require_the_judge(policy: EvaluationPolicy) -> None:
    assert policy.thresholds["semantic_groundedness"].requires_judge
    assert policy.thresholds["unsupported_claim_rate"].requires_judge


def test_quality_and_cost_gates_are_live_only(policy: EvaluationPolicy) -> None:
    """The deterministic fake cannot produce evidence about answer quality.

    Gating an offline run on it would either fail every CI run or force the fake
    to be taught the right answers, which is manufacturing a passing score.
    """
    for metric in (
        "disposition_accuracy",
        "refusal_accuracy",
        "expected_document_citation_recall",
        "latency_p95_ms",
        "total_tokens_p95",
    ):
        assert policy.thresholds[metric].modes == (ExecutionMode.LIVE,), metric


def test_structural_gates_apply_offline(policy: EvaluationPolicy) -> None:
    applicable = policy.applicable(ExecutionMode.FAKE, judge_enabled=False)
    assert set(applicable) == {
        "schema_validity_rate",
        "citation_containment_rate",
        "prohibited_content_rate",
        "provider_failure_rate",
    }


def test_live_with_judge_adds_exactly_the_judge_gates(policy: EvaluationPolicy) -> None:
    without = set(policy.applicable(ExecutionMode.LIVE, judge_enabled=False))
    with_judge = set(policy.applicable(ExecutionMode.LIVE, judge_enabled=True))
    assert with_judge - without == {"semantic_groundedness", "unsupported_claim_rate"}


# --- gate arithmetic --------------------------------------------------------


def test_a_missing_metric_is_incomplete_and_never_passes(policy: EvaluationPolicy) -> None:
    results = evaluate_gates(
        policy,
        {
            "schema_validity_rate": None,
            "citation_containment_rate": 1.0,
            "prohibited_content_rate": 0.0,
            "provider_failure_rate": 0.0,
        },
        ExecutionMode.FAKE,
        judge_enabled=False,
    )
    schema = next(result for result in results if result.metric == "schema_validity_rate")
    assert schema.status is GateStatus.INCOMPLETE
    assert not schema.passed
    assert overall_status(results) is GateStatus.INCOMPLETE


def test_a_metric_absent_from_the_mapping_is_also_incomplete(policy: EvaluationPolicy) -> None:
    results = evaluate_gates(policy, {}, ExecutionMode.FAKE, judge_enabled=False)
    assert all(result.status is GateStatus.INCOMPLETE for result in results)


def test_a_breach_fails(policy: EvaluationPolicy) -> None:
    results = evaluate_gates(
        policy,
        {
            "schema_validity_rate": 1.0,
            "citation_containment_rate": 0.9,
            "prohibited_content_rate": 0.0,
            "provider_failure_rate": 0.0,
        },
        ExecutionMode.FAKE,
        judge_enabled=False,
    )
    assert overall_status(results) is GateStatus.FAIL


def test_failure_dominates_incompleteness(policy: EvaluationPolicy) -> None:
    """A definite breach is information; a missing metric is the absence of it."""
    results = evaluate_gates(
        policy,
        {
            "schema_validity_rate": None,
            "citation_containment_rate": 0.5,
            "prohibited_content_rate": 0.0,
            "provider_failure_rate": 0.0,
        },
        ExecutionMode.FAKE,
        judge_enabled=False,
    )
    assert overall_status(results) is GateStatus.FAIL


def test_all_satisfied_passes(policy: EvaluationPolicy) -> None:
    results = evaluate_gates(
        policy,
        {
            "schema_validity_rate": 1.0,
            "citation_containment_rate": 1.0,
            "prohibited_content_rate": 0.0,
            "provider_failure_rate": 0.0,
        },
        ExecutionMode.FAKE,
        judge_enabled=False,
    )
    assert overall_status(results) is GateStatus.PASS


def test_an_external_reason_forces_incomplete(policy: EvaluationPolicy) -> None:
    """A live report from a dirty tree cannot be attributed to a commit."""
    results = evaluate_gates(
        policy,
        {
            "schema_validity_rate": 1.0,
            "citation_containment_rate": 1.0,
            "prohibited_content_rate": 0.0,
            "provider_failure_rate": 0.0,
        },
        ExecutionMode.FAKE,
        judge_enabled=False,
    )
    assert overall_status(results, extra_incomplete=True) is GateStatus.INCOMPLETE


# --- threshold shape --------------------------------------------------------


def test_a_threshold_must_set_exactly_one_bound() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        Threshold(modes=(ExecutionMode.FAKE,), requires_judge=False, rationale="x")
    with pytest.raises(ValueError, match="exactly one"):
        Threshold(
            modes=(ExecutionMode.FAKE,),
            requires_judge=False,
            minimum=1.0,
            maximum=1.0,
            rationale="x",
        )


def test_the_policy_hash_is_of_the_file_bytes(policy: EvaluationPolicy) -> None:
    import hashlib

    assert policy.content_sha256 == hashlib.sha256(DEFAULT_POLICY_PATH.read_bytes()).hexdigest()


def test_a_malformed_policy_is_a_configuration_error(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text("{")
    with pytest.raises(ConfigurationError, match="not valid JSON"):
        load_policy(path)


def test_an_unknown_policy_field_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(DEFAULT_POLICY_PATH.read_text())
    payload["unexpected"] = True
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ConfigurationError, match="failed schema validation"):
        load_policy(path)
