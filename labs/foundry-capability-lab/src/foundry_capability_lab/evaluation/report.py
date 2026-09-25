"""Report construction and rendering.

REDACTION IS STRUCTURAL, NOT EDITORIAL
--------------------------------------
Neither the observation text nor the model's `rationale` reaches a report,
because neither is ever placed in a `CaseOutcome` in the first place. There is no
"remember to strip it" step that a future edit could forget: the field simply
does not exist on the object being serialised. `render_markdown` and
`build_report` read only from `CaseOutcome`, `EvaluationMetrics` and
`GateResult`, all of which are closed Pydantic models holding identifiers,
enums, booleans and numbers.

What a report contains: case ids, expected and actual decisions, whether the
cited evidence was supported, how many identifiers were fabricated, failure
categories, latency and token aggregates, and the gate verdicts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from foundry_capability_lab.evaluation.metrics import CaseOutcome, EvaluationMetrics
from foundry_capability_lab.evaluation.thresholds import GateResult, gates_passed

DEFAULT_ARTIFACTS_DIR = Path(__file__).resolve().parents[3] / "artifacts"


def build_report(
    metrics: EvaluationMetrics,
    gate_results: list[GateResult],
    outcomes: list[CaseOutcome],
    *,
    deployment: str,
    dataset_name: str,
    threshold_version: int,
    provider_label: str,
    generated_at: datetime | None = None,
    operationally_complete: bool = True,
    aborted_on: str | None = None,
    planned_attempts: int | None = None,
    inter_attempt_delay_seconds: float = 0.0,
) -> dict[str, Any]:
    """Assemble the machine-readable report.

    `operationally_complete` is carried explicitly and prominently. An aborted
    run still produces a report — it is the diagnostic — but it must never be
    read as a quality measurement, so the distinction is a top-level field
    rather than something a reader has to infer from the attempt count.
    """
    timestamp = generated_at or datetime.now(UTC)

    return {
        "schema_version": 1,
        "generated_at": timestamp.isoformat(),
        "run": {
            "provider": provider_label,
            "deployment": deployment,
            "dataset": dataset_name,
            "threshold_version": threshold_version,
            "cases": metrics.cases,
            "repetitions": metrics.repetitions,
            "attempts": metrics.attempts,
            "planned_attempts": planned_attempts
            if planned_attempts is not None
            else metrics.attempts,
            "valid_outputs": metrics.valid_outputs,
            "retries": 0,
            "inter_attempt_delay_seconds": inter_attempt_delay_seconds,
        },
        "operationally_complete": operationally_complete,
        "aborted_on": aborted_on,
        "metrics": metrics.model_dump(),
        "gates": [result.model_dump() for result in gate_results],
        "passed": gates_passed(gate_results),
        # Per-case detail, redacted by construction (see module docstring).
        "cases": [
            {
                "case_id": outcome.case_id,
                "repetition": outcome.repetition,
                "schema_valid": outcome.schema_valid,
                "expected_classification": outcome.expected_classification,
                "actual_classification": outcome.actual_classification,
                "classification_correct": outcome.classification_correct,
                "expected_severity": outcome.expected_severity,
                "actual_severity": outcome.actual_severity,
                "severity_correct": outcome.severity_correct,
                "expected_requires_escalation": outcome.expected_requires_escalation,
                "actual_requires_escalation": outcome.actual_requires_escalation,
                "escalation_correct": outcome.escalation_correct,
                "evidence_valid": outcome.evidence_valid,
                "fabricated_evidence_count": outcome.fabricated_evidence_count,
                "failure_category": outcome.failure_category,
                "latency_ms": round(outcome.latency_ms, 2),
                "total_tokens": outcome.total_tokens,
            }
            for outcome in outcomes
        ],
    }


# Metrics whose value is a proportion in [0, 1] and reads best as a percentage.
# Chosen by NAME, not by magnitude: a sub-millisecond latency is still a
# duration, and formatting it as "1.7%" because it happens to be below 1.0 is
# how a report starts lying about its own units.
RATIO_METRIC_SUFFIXES = ("_rate", "_accuracy", "_consistency")


def is_ratio_metric(name: str) -> bool:
    return name.endswith(RATIO_METRIC_SUFFIXES)


def format_metric(name: str, value: float) -> str:
    """Render a metric in its own units."""
    if is_ratio_metric(name):
        return f"{value:.1%}"
    if name.endswith("_ms"):
        return f"{value:.0f} ms"
    return f"{value:g}"


def evidence_cell(case: dict[str, Any]) -> str:
    """Render the evidence column: only meaningful when the response validated."""
    if not case["schema_valid"]:
        return "—"
    return "ok" if case["evidence_valid"] else "invalid"


def render_markdown(report: dict[str, Any]) -> str:
    """Render a concise human summary. Deliberately short enough to read."""
    metrics = report["metrics"]
    run = report["run"]
    complete = report.get("operationally_complete", True)
    verdict = "PASS" if report["passed"] else "FAIL"
    if not complete:
        verdict = "INCOMPLETE"

    lines = [
        f"# Foundry risk evaluation — {verdict}",
        "",
        f"- generated: `{report['generated_at']}`",
        f"- provider: `{run['provider']}` · deployment: `{run['deployment']}`",
        f"- dataset: `{run['dataset']}` · thresholds: v{run['threshold_version']}",
        f"- {run['cases']} cases x {run['repetitions']} repetitions "
        f"= {run['attempts']} of {run['planned_attempts']} planned attempts, "
        f"{run['valid_outputs']} schema-valid, {run['retries']} retries",
        f"- pacing: {run['inter_attempt_delay_seconds']}s between attempts",
    ]

    if not complete:
        lines += [
            "",
            f"> **Operationally incomplete — aborted on `{report['aborted_on']}`.**",
            "> The run stopped before every planned attempt was made, so the "
            "metrics below describe only what completed and are NOT a quality "
            "measurement. Fix the operational cause and re-run.",
        ]

    lines += [
        "",
        "## Gates",
        "",
        "| Metric | Value | Bound | Gated | Result |",
        "|---|---|---|---|---|",
    ]

    for gate in report["gates"]:
        shown = format_metric(gate["metric"], gate["value"])
        lines.append(
            f"| {gate['metric']} | {shown} | {gate['bound']} | "
            f"{'yes' if gate['gated'] else 'no'} | "
            f"{'pass' if gate['passed'] else 'FAIL'} |"
        )

    lines += [
        "",
        "## Telemetry",
        "",
        f"- latency p50 / p95: {metrics['latency_p50_ms']:.0f} ms / "
        f"{metrics['latency_p95_ms']:.0f} ms",
        f"- tokens in / out / total: {metrics['input_tokens']} / "
        f"{metrics['output_tokens']} / {metrics['total_tokens']}",
        f"- fabricated evidence identifiers: {metrics['fabricated_evidence_count']}",
    ]

    if metrics["failure_counts"]:
        lines.append("- failures by category:")
        for category, count in metrics["failure_counts"].items():
            lines.append(f"  - {category}: {count}")
    else:
        lines.append("- failures: none")

    incorrect = [
        case
        for case in report["cases"]
        if not case["schema_valid"] or not case["classification_correct"]
    ]
    lines += ["", "## Cases needing attention", ""]
    if not incorrect:
        lines.append("None: every attempt was schema-valid and correctly classified.")
    else:
        lines += [
            "| Case | Rep | Valid | Expected | Actual | Evidence |",
            "|---|---|---|---|---|---|",
        ]
        for case in incorrect:
            lines.append(
                f"| {case['case_id']} | {case['repetition']} | "
                f"{'yes' if case['schema_valid'] else 'no'} | "
                f"{case['expected_classification']} | "
                f"{case['actual_classification'] or case['failure_category'] or '—'} | "
                f"{evidence_cell(case)} |"
            )

    lines += [
        "",
        "---",
        "",
        "Observation text and model rationale are deliberately excluded from this "
        "report; only identifiers, decisions and aggregates are recorded.",
        "",
    ]
    return "\n".join(lines)


def write_reports(report: dict[str, Any], artifacts_dir: Path | None = None) -> tuple[Path, Path]:
    """Write the JSON and Markdown reports. Returns (json_path, markdown_path)."""
    directory = DEFAULT_ARTIFACTS_DIR if artifacts_dir is None else artifacts_dir
    directory.mkdir(parents=True, exist_ok=True)

    json_path = directory / "evaluation-report.json"
    markdown_path = directory / "evaluation-report.md"

    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    markdown_path.write_text(render_markdown(report))

    return json_path, markdown_path
