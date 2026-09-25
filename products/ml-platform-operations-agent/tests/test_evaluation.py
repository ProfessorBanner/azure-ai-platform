"""The evaluation harness itself: cases, scorers and gates."""

from __future__ import annotations

import pytest

from evaluation.cli import evaluate, load_cases, run_case
from evaluation.scorers import GATES, QUALITY_SCORERS, SAFETY_SCORERS, score_all
from ml_platform_operations_agent.adapters.fake import SCENARIOS


def test_every_case_names_a_real_scenario() -> None:
    for case in load_cases():
        assert case["scenario"] in SCENARIOS, f"{case['id']} names an unknown scenario"


def test_case_ids_are_unique() -> None:
    ids = [case["id"] for case in load_cases()]
    assert len(ids) == len(set(ids))


def test_every_scenario_is_covered_by_a_case() -> None:
    """A fixture nothing exercises is a fixture that can rot unnoticed."""
    covered = {case["scenario"] for case in load_cases()}
    assert covered == set(SCENARIOS)


def test_all_gates_pass() -> None:
    report = evaluate(load_cases())
    failures = [f"{f['case']}/{f['scorer']}: {f['detail']}" for f in report["failures"]]
    assert report["all_gates_passed"], "gate failures:\n" + "\n".join(failures)


def test_safety_gates_are_required_at_one_hundred_percent() -> None:
    """A single safety failure is a defect, not a score to average."""
    for scorer in SAFETY_SCORERS:
        name = scorer(run_case(load_cases()[0]), load_cases()[0].get("expected", {})).name
        assert GATES[name] == 1.0, f"{name} must be a 100% gate"


def test_quality_gates_are_proportional() -> None:
    report = evaluate(load_cases())
    proportional = {row["gate"] for row in report["gates"] if row["required"] < 1.0}
    assert proportional == {"tool_selection", "root_cause_accuracy", "evidence_completeness"}


def test_quality_gate_thresholds_match_the_specification() -> None:
    assert GATES["tool_selection"] == 0.90
    assert GATES["root_cause_accuracy"] == 0.85
    assert GATES["evidence_completeness"] == 0.90


def test_determinism_is_measured_not_assumed() -> None:
    report = evaluate(load_cases())
    assert report["non_deterministic_cases"] == []
    assert any(row["gate"] == "determinism" for row in report["gates"])


def test_evaluation_is_reproducible_across_invocations() -> None:
    first = evaluate(load_cases())
    second = evaluate(load_cases())
    assert first["gates"] == second["gates"]


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["id"])
def test_each_case_passes_every_scorer(case: dict[str, object]) -> None:
    diagnosis = run_case(case)
    expected = case.get("expected", {})
    assert isinstance(expected, dict)
    for result in score_all(diagnosis, expected):
        assert result.passed, f"{case['id']} failed {result.name}: {result.detail}"


def test_scorer_count_is_stable() -> None:
    """19.2d wraps these for mlflow.genai.evaluate; a silent addition or
    removal would change what is gated without a decision."""
    assert len(SAFETY_SCORERS) == 9
    assert len(QUALITY_SCORERS) == 3
