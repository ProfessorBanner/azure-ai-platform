"""The MLflow scorer wrappers, and that they agree with the authoritative gates."""

from __future__ import annotations

from typing import Any

import pytest
from mlflow.entities import Feedback

from evaluation.cli import load_cases, run_case
from evaluation.mlflow_runner import EXPERIMENT, build_data, plan, predict, source_hash
from evaluation.mlflow_scorers import ALL_SCORERS, DELEGATES


def _feedback(scorer_obj: Any, outputs: Any, expectations: Any) -> Feedback:
    result = scorer_obj(outputs=outputs, expectations=expectations)
    assert isinstance(result, Feedback)
    return result


# --- the wrappers adapt, they do not reimplement -----------------------------


def test_every_mlflow_scorer_delegates_to_an_authoritative_gate() -> None:
    """Two copies of a gate drift, and the drifted copy is the one nobody runs."""
    wrapped = {s.name for s in ALL_SCORERS} - {"deterministic_output"}
    assert wrapped == set(DELEGATES)


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["id"])
def test_wrappers_agree_with_the_standalone_gates(case: dict[str, Any]) -> None:
    """Case by case. A divergence fails here rather than publishing two
    different numbers."""
    diagnosis = run_case(case)
    expected = case.get("expected", {})
    outputs = {"diagnosis": diagnosis.model_dump(mode="json")}

    for name, delegate in DELEGATES.items():
        authoritative = delegate(diagnosis, expected)
        wrapper = next(s for s in ALL_SCORERS if s.name == name)
        feedback = _feedback(wrapper, outputs, expected)
        assert feedback.value == authoritative.passed, f"{case['id']}/{name} diverged"


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["id"])
def test_every_scorer_passes_on_every_case(case: dict[str, Any]) -> None:
    data = build_data((case,))[0]
    outputs = predict(**data["inputs"])
    for scorer_obj in ALL_SCORERS:
        feedback = _feedback(scorer_obj, outputs, data["expectations"])
        assert feedback.value is True, f"{case['id']}/{scorer_obj.name}: {feedback.rationale}"


# --- fail closed ------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, "not a diagnosis", 42, {}, {"diagnosis": {"nope": 1}}])
def test_malformed_output_fails_closed(bad: Any) -> None:
    """A scorer that raised would abort every remaining case; one that passed
    would be worse."""
    for scorer_obj in ALL_SCORERS:
        if scorer_obj.name == "deterministic_output":
            continue
        feedback = _feedback(scorer_obj, bad, {})
        assert feedback.value is False


def test_missing_expectations_do_not_raise() -> None:
    diagnosis = run_case(load_cases()[0])
    outputs = {"diagnosis": diagnosis.model_dump(mode="json")}
    for scorer_obj in ALL_SCORERS:
        _feedback(scorer_obj, outputs, None)


def test_determinism_scorer_requires_the_target_to_have_checked() -> None:
    assert (
        _feedback(next(s for s in ALL_SCORERS if s.name == "deterministic_output"), {}, {}).value
        is False
    )


# --- the target -------------------------------------------------------------


def test_target_returns_a_serialisable_diagnosis() -> None:
    result = predict(
        case_id="healthy", scenario="healthy", question="why did it degrade?", window_days=7
    )
    assert "diagnosis" in result
    assert result["diagnosis"]["degradation_status"] == "not_degraded"


def test_target_uses_fake_adapters_only() -> None:
    """No live Databricks call and no model call."""
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "evaluation/mlflow_runner.py"
    ).read_text(encoding="utf-8")
    for marker in ("WorkspaceClient", "MlflowClient", "DatabricksModelRegistry", "serving"):
        assert marker not in source


def test_data_uses_nested_inputs_and_expectations() -> None:
    """MLflow requires the nested shape and unpacks inputs as kwargs."""
    rows = build_data(load_cases())
    assert len(rows) == 19
    for row in rows:
        assert set(row) == {"inputs", "expectations"}
        assert set(row["inputs"]) == {"case_id", "scenario", "question", "window_days"}


def test_target_signature_matches_the_input_keys() -> None:
    """MLflow calls predict_fn with **inputs, not with a dict."""
    import inspect

    assert set(inspect.signature(predict).parameters) == {
        "case_id",
        "scenario",
        "question",
        "window_days",
    }


def test_determinism_is_recorded_for_every_case() -> None:
    for row in build_data(load_cases()):
        assert row["expectations"]["deterministic"] is True


# --- the plan ---------------------------------------------------------------


def test_plan_creates_nothing_and_names_the_authorised_experiment() -> None:
    report = plan(load_cases(), "plan")
    assert report["experiment"] == EXPERIMENT
    assert report["case_count"] == 19
    assert report["expected_root_traces"] == 19
    assert report["expected_model_calls"] == 0
    assert report["llm_judges"] == 0
    assert report["evidence"] == "synthetic"
    assert len(report["scorer_names"]) == 14


def test_plan_records_dirty_tree_provenance() -> None:
    """A bare SHA would claim the code that ran is the code at that commit."""
    report = plan(load_cases(), "plan")
    assert report["base_commit"]
    assert report["working_tree_dirty"] in {"true", "false"}
    assert len(report["source_hash"]) == 16


def test_source_hash_is_stable() -> None:
    assert source_hash() == source_hash()


def test_plan_lists_what_will_not_be_created() -> None:
    forbidden = set(plan(load_cases(), "plan")["will_not_create"])
    assert "labeling session" in forbidden
    assert "scheduled scorers" in forbidden
    assert "online monitoring" in forbidden
