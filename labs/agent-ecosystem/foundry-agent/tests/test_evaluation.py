"""The lab evaluation suite: dataset, metrics, gates and report.

Tests of a test harness, which is worth stating: the failure mode of a harness
is that it goes green while measuring nothing. These are written to catch that —
a metric that cannot fail, a gate that does not apply, a denominator that
quietly empties.
"""

from __future__ import annotations

import json

import pytest

from foundry_agent_lab._product import ensure_product_importable
from foundry_agent_lab.docs_search import _product_search_tool
from foundry_agent_lab.evaluation.dataset import (
    DATASET_PATH,
    GATES_PATH,
    REQUIRED_CATEGORIES,
    DatasetError,
    load_dataset,
)
from foundry_agent_lab.evaluation.metrics import LabCaseOutcome, compute_metrics
from foundry_agent_lab.evaluation.report import ReportInputs, build_report, render_markdown
from foundry_agent_lab.evaluation.runner import run_dataset
from foundry_agent_lab.governance import GovernedToolGateway

ensure_product_importable()

from platform_engineering_assistant.evaluation.policy import (  # noqa: E402
    ExecutionMode,
    GateStatus,
    evaluate_gates,
    load_policy,
    overall_status,
)

DATASET = load_dataset()
GATES = load_policy(GATES_PATH)


@pytest.fixture(scope="module")
def outcomes():  # type: ignore[no-untyped-def]
    gateway = GovernedToolGateway(_product_search_tool()._index)
    return run_dataset(DATASET, gateway)


@pytest.fixture(scope="module")
def metrics(outcomes):  # type: ignore[no-untyped-def]
    return compute_metrics(outcomes.outcomes, outcomes.planned_cases)


# --- 1. the dataset ----------------------------------------------------------


def test_the_dataset_loads_and_hashes_its_bytes() -> None:
    assert DATASET.dataset_id == "foundry_agent_lab_v1"
    assert len(DATASET.content_sha256) == 64


@pytest.mark.parametrize("category", sorted(REQUIRED_CATEGORIES))
def test_every_required_category_is_present(category: str) -> None:
    """A category losing its last case is a load failure, not a smaller suite."""
    assert any(case.category == category for case in DATASET.cases)


def test_every_case_records_why_it_exists() -> None:
    for case in DATASET.cases:
        assert len(case.rationale) > 40, f"{case.case_id} has no written provenance"


def test_the_dataset_states_that_it_is_not_a_benchmark() -> None:
    assert "NOT AN INDEPENDENT BENCHMARK" in " ".join(DATASET.limitations)


def test_a_set_missing_a_category_is_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    raw = json.loads(DATASET_PATH.read_text())
    raw["cases"] = [c for c in raw["cases"] if c["category"] != "approval-required"]
    path = tmp_path / "d.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(DatasetError, match="approval-required"):
        load_dataset(path)


def test_an_approval_case_may_not_expect_execution(tmp_path) -> None:  # type: ignore[no-untyped-def]
    raw = json.loads(DATASET_PATH.read_text())
    for case in raw["cases"]:
        if case["category"] == "approval-required":
            case["expects_execution"] = True
    path = tmp_path / "d.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(DatasetError):
        load_dataset(path)


# --- 2. the run --------------------------------------------------------------


def test_every_case_runs_without_an_exception_escaping(outcomes) -> None:  # type: ignore[no-untyped-def]
    assert outcomes.complete
    assert [o.case_id for o in outcomes.outcomes if o.error_class] == []


def test_no_state_changing_tool_ever_executed(metrics) -> None:  # type: ignore[no-untyped-def]
    assert metrics.unauthorised_execution_count == 0


def test_the_approval_case_stopped_and_bound_its_approval(outcomes) -> None:  # type: ignore[no-untyped-def]
    case = next(o for o in outcomes.outcomes if o.case_id == "LAB-004")
    assert case.observed_outcome == "approval_required"
    assert case.executed is False
    assert case.approval_id and case.argument_fingerprint


def test_the_adversarial_case_was_denied_and_never_ran(outcomes) -> None:  # type: ignore[no-untyped-def]
    case = next(o for o in outcomes.outcomes if o.case_id == "LAB-005")
    assert case.observed_outcome == "denied"
    assert case.executed is False


def test_every_trajectory_is_valid(metrics) -> None:  # type: ignore[no-untyped-def]
    assert metrics.trajectory_validity_rate == 1.0


def test_the_approval_denominator_is_not_empty(metrics) -> None:  # type: ignore[no-untyped-def]
    """A gate over zero cases would pass vacuously."""
    assert metrics.approval_compliance_cases >= 1
    assert metrics.policy_compliance_cases >= 1


# --- 3. the metrics must be able to fail ------------------------------------


def failing(**overrides: object) -> LabCaseOutcome:
    base: dict[str, object] = {
        "case_id": "SYN",
        "category": "approval-required",
        "expected_outcome": "approval_required",
        "observed_outcome": "answered",
    }
    base.update(overrides)
    return LabCaseOutcome(**base)  # type: ignore[arg-type]


def test_an_unauthorised_execution_is_counted() -> None:
    assert (
        compute_metrics([failing(unauthorised_execution=True)], 1).unauthorised_execution_count == 1
    )


def test_an_approval_that_did_not_stop_scores_false() -> None:
    assert compute_metrics([failing()], 1).approval_compliance_rate == 0.0


def test_a_trajectory_defect_scores_false() -> None:
    m = compute_metrics([failing(correlated=True, trajectory_defects=("boom",))], 1)
    assert m.trajectory_validity_rate == 0.0


def test_an_absent_record_scores_false_rather_than_passing() -> None:
    assert compute_metrics([failing(correlated=False)], 1).trajectory_validity_rate == 0.0


def test_an_empty_population_is_not_measured_rather_than_perfect() -> None:
    m = compute_metrics([], 0)
    assert m.approval_compliance_rate is None
    assert m.task_success_rate is None


# --- 4. the gates ------------------------------------------------------------


def test_the_offline_run_passes_every_applicable_gate(metrics) -> None:  # type: ignore[no-untyped-def]
    gates = evaluate_gates(GATES, metrics.as_observed(), ExecutionMode.FAKE, judge_enabled=False)
    assert gates, "a run with no applicable gate would pass vacuously"
    assert overall_status(gates) is GateStatus.PASS


def test_the_model_side_gates_are_live_only() -> None:
    fake = set(GATES.applicable(ExecutionMode.FAKE, judge_enabled=False))
    live = set(GATES.applicable(ExecutionMode.LIVE, judge_enabled=False))
    for metric in ("tool_selection_accuracy", "unnecessary_tool_call_rate"):
        assert metric not in fake
        assert metric in live


def test_the_control_gates_apply_in_both_modes() -> None:
    fake = set(GATES.applicable(ExecutionMode.FAKE, judge_enabled=False))
    for control in (
        "unauthorised_execution_count",
        "approval_compliance_rate",
        "policy_compliance_rate",
        "trajectory_validity_rate",
        "task_success_rate",
    ):
        assert control in fake


def test_a_single_unauthorised_execution_fails_the_run(metrics) -> None:  # type: ignore[no-untyped-def]
    observed = metrics.as_observed()
    observed["unauthorised_execution_count"] = 1.0
    gates = evaluate_gates(GATES, observed, ExecutionMode.FAKE, judge_enabled=False)
    assert overall_status(gates) is GateStatus.FAIL


def test_an_unmeasured_metric_is_incomplete_never_a_pass() -> None:
    gates = evaluate_gates(
        GATES, {"unauthorised_execution_count": None}, ExecutionMode.FAKE, judge_enabled=False
    )
    assert overall_status(gates) is not GateStatus.PASS


def test_every_gate_records_a_rationale() -> None:
    for name, threshold in GATES.thresholds.items():
        assert len(threshold.rationale) > 60, f"{name} has no rationale worth reading"


# --- 5. the report -----------------------------------------------------------


def report(metrics, outcomes) -> ReportInputs:  # type: ignore[no-untyped-def]
    gates = evaluate_gates(GATES, metrics.as_observed(), ExecutionMode.FAKE, judge_enabled=False)
    applicable = set(GATES.applicable(ExecutionMode.FAKE, judge_enabled=False))
    return ReportInputs(
        dataset_id=DATASET.dataset_id,
        dataset_version=DATASET.dataset_version,
        dataset_sha256=DATASET.content_sha256,
        gates_id=GATES.policy_id,
        gates_version=GATES.policy_version,
        execution_mode="fake",
        agent_name="eval-agent",
        agent_version="eval-1",
        generated_at="2026-09-02T00:00:00+00:00",
        metrics=metrics,
        gates=gates,
        outcomes=outcomes.outcomes,
        status=overall_status(gates).value,
        unassessed=tuple(sorted(set(GATES.thresholds) - applicable)),
        limitations=DATASET.limitations,
    )


def test_the_report_is_machine_readable_and_states_its_schema(metrics, outcomes) -> None:  # type: ignore[no-untyped-def]
    payload = build_report(report(metrics, outcomes))
    assert payload["report_type"] == "foundry_agent_lab_evaluation"
    assert payload["status"] == "pass"
    assert len(payload["cases"]) == len(DATASET.cases)
    assert payload["provenance"]["dataset_sha256"] == DATASET.content_sha256


def test_the_report_names_the_gates_it_did_not_assess(metrics, outcomes) -> None:  # type: ignore[no-untyped-def]
    rendered = render_markdown(report(metrics, outcomes))
    assert "Not assessed in this run" in rendered
    assert "`tool_selection_accuracy`" in rendered


def test_no_report_contains_a_question(metrics, outcomes) -> None:  # type: ignore[no-untyped-def]
    inputs = report(metrics, outcomes)
    rendered = render_markdown(inputs) + json.dumps(build_report(inputs))
    for case in DATASET.cases:
        assert case.question not in rendered, f"{case.case_id} leaked its question"
