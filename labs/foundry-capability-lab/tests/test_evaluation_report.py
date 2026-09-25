"""Report construction, rendering and — above all — redaction."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from foundry_capability_lab.errors import FailureCategory
from foundry_capability_lab.evaluation.metrics import compute_metrics
from foundry_capability_lab.evaluation.report import (
    build_report,
    format_metric,
    render_markdown,
    write_reports,
)
from foundry_capability_lab.evaluation.thresholds import (
    Threshold,
    ThresholdSet,
    evaluate_gates,
)
from tests.evaluation_fakes import make_outcome

SECRET_OBSERVATION = "customer bank account 12345678 was leaked"
SECRET_RATIONALE = "the model explained something confidential here"

THRESHOLDS = ThresholdSet(
    version=7,
    thresholds={
        "classification_accuracy": Threshold(gated=True, minimum=0.5),
        "latency_p95_ms": Threshold(gated=False, maximum=10.0),
    },
)


def build(outcomes: list[Any] | None = None) -> dict[str, Any]:
    resolved = outcomes or [make_outcome(case_id="A", repetition=r) for r in (1, 2)]
    metrics = compute_metrics(resolved)
    return build_report(
        metrics,
        evaluate_gates(metrics, THRESHOLDS),
        resolved,
        deployment="gpt-4-1-mini",
        dataset_name="risk_evaluation_cases.jsonl",
        threshold_version=THRESHOLDS.version,
        provider_label="foundry",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_report_records_the_run_shape_including_zero_retries() -> None:
    report = build()
    assert report["run"]["retries"] == 0
    assert report["run"]["attempts"] == 2
    assert report["run"]["threshold_version"] == 7
    assert report["run"]["deployment"] == "gpt-4-1-mini"


def test_report_contains_gates_and_a_verdict() -> None:
    report = build()
    assert {gate["metric"] for gate in report["gates"]} == set(THRESHOLDS.thresholds)
    assert report["passed"] is True


def test_ungated_breach_does_not_change_the_verdict() -> None:
    """latency_p95_ms is bounded at 10ms here and outcomes are 10ms, so it passes;
    push it over and the verdict must still be a pass because it is ungated."""
    outcomes = [make_outcome(case_id="A", repetition=r, latency_ms=5000.0) for r in (1, 2)]
    report = build(outcomes)
    latency_gate = next(g for g in report["gates"] if g["metric"] == "latency_p95_ms")
    assert latency_gate["passed"] is False
    assert report["passed"] is True


def test_per_case_records_hold_decisions_not_text() -> None:
    report = build()
    case = report["cases"][0]
    assert case["case_id"] == "A"
    assert "rationale" not in case
    assert "observation" not in case
    assert set(case) == {
        "case_id",
        "repetition",
        "schema_valid",
        "expected_classification",
        "actual_classification",
        "classification_correct",
        "expected_severity",
        "actual_severity",
        "severity_correct",
        "expected_requires_escalation",
        "actual_requires_escalation",
        "escalation_correct",
        "evidence_valid",
        "fabricated_evidence_count",
        "failure_category",
        "latency_ms",
        "total_tokens",
    }


def test_serialised_report_contains_no_observation_or_rationale_text() -> None:
    """The end-to-end redaction guarantee, asserted on the rendered JSON."""
    serialised = json.dumps(build(), default=str)
    assert SECRET_OBSERVATION not in serialised
    assert SECRET_RATIONALE not in serialised
    # The fake's rationale is a distinctive string; it must not appear either.
    assert "Synthetic rationale" not in serialised


def test_markdown_contains_no_free_text_and_states_the_exclusion() -> None:
    markdown = render_markdown(build())
    assert "Synthetic rationale" not in markdown
    assert SECRET_OBSERVATION not in markdown
    assert "deliberately excluded" in markdown


def test_markdown_reports_the_verdict_and_denominators() -> None:
    markdown = render_markdown(build())
    assert "PASS" in markdown
    assert "2 of 2 planned attempts" in markdown
    assert "0 retries" in markdown


def test_markdown_lists_failing_cases_by_id() -> None:
    outcomes = [
        make_outcome(case_id="GOOD", repetition=1),
        make_outcome(
            case_id="BAD",
            repetition=1,
            schema_valid=False,
            failure_category=FailureCategory.TIMEOUT,
        ),
    ]
    markdown = render_markdown(build(outcomes))
    assert "BAD" in markdown
    assert "timeout" in markdown


def test_markdown_says_so_when_nothing_needs_attention() -> None:
    assert "None: every attempt" in render_markdown(build())


def test_write_reports_creates_both_files(tmp_path: Path) -> None:
    json_path, markdown_path = write_reports(build(), tmp_path)

    assert json_path.exists() and markdown_path.exists()
    assert json_path.parent == tmp_path
    reloaded = json.loads(json_path.read_text())
    assert reloaded["run"]["attempts"] == 2
    assert markdown_path.read_text().startswith("# Foundry risk evaluation")


def test_write_reports_creates_a_missing_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "artifacts"
    json_path, _ = write_reports(build(), target)
    assert json_path.exists()


def test_written_json_is_stable_across_runs(tmp_path: Path) -> None:
    """Same inputs, same bytes: reports diff cleanly."""
    report = build()
    first, _ = write_reports(report, tmp_path / "a")
    second, _ = write_reports(report, tmp_path / "b")
    assert first.read_text() == second.read_text()


# --- metric formatting ------------------------------------------------------


def test_ratio_metrics_render_as_percentages() -> None:
    assert format_metric("classification_accuracy", 0.917) == "91.7%"
    assert format_metric("provider_error_rate", 0.0) == "0.0%"
    assert format_metric("repeated_call_consistency", 1.0) == "100.0%"


def test_latency_renders_as_a_duration_not_a_percentage() -> None:
    """A sub-millisecond latency is still a duration.

    Formatting by magnitude once rendered 0.017 ms as '1.7%', which is a report
    misstating its own units.
    """
    assert format_metric("latency_p95_ms", 0.017) == "0 ms"
    assert format_metric("latency_p95_ms", 1420.0) == "1420 ms"


def test_gate_table_labels_latency_in_milliseconds() -> None:
    markdown = render_markdown(build())
    latency_row = next(
        line for line in markdown.splitlines() if line.startswith("| latency_p95_ms")
    )
    assert "ms" in latency_row
    assert "%" not in latency_row
