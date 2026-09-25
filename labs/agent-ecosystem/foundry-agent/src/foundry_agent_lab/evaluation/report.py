"""The machine-readable evaluation report, plus a reviewer-facing summary.

Identifiers, versions, hashes, enums, counts and rates. No question, no answer,
no instructions, no arguments — the outcome type has no field for any of them.

Every report states which gates did NOT apply. A fake-mode run does not evaluate
the model-side gates at all, and showing five passes without saying two were
never assessed would be misleading by omission.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from foundry_agent_lab.evaluation.metrics import LabCaseOutcome, LabMetrics

REPORT_SCHEMA_VERSION = 1
JSON_NAME = "foundry-agent-lab-report.json"
MARKDOWN_NAME = "foundry-agent-lab-report.md"


@dataclass(frozen=True, slots=True)
class ReportInputs:
    dataset_id: str
    dataset_version: int
    dataset_sha256: str
    gates_id: str
    gates_version: int
    execution_mode: str
    agent_name: str
    agent_version: str
    generated_at: str
    metrics: LabMetrics
    gates: list[Any]
    outcomes: list[LabCaseOutcome]
    status: str
    unassessed: tuple[str, ...]
    limitations: tuple[str, ...]


def _case(outcome: LabCaseOutcome) -> dict[str, Any]:
    return {
        "case_id": outcome.case_id,
        "category": outcome.category,
        "expected_outcome": outcome.expected_outcome,
        "observed_outcome": outcome.observed_outcome,
        "task_successful": outcome.task_successful,
        "policy_compliant": outcome.policy_compliant,
        "approval_compliant": outcome.approval_compliant,
        "trajectory_valid": outcome.trajectory_valid,
        "trajectory_defects": list(outcome.trajectory_defects),
        "unauthorised_execution": outcome.unauthorised_execution,
        "observed_tool": outcome.observed_tool,
        "executed": outcome.executed,
        "approval_id": outcome.approval_id,
        "error_class": outcome.error_class,
    }


def build_report(inputs: ReportInputs) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "foundry_agent_lab_evaluation",
        "status": inputs.status,
        "execution_mode": inputs.execution_mode,
        "provenance": {
            "dataset_id": inputs.dataset_id,
            "dataset_version": inputs.dataset_version,
            "dataset_sha256": inputs.dataset_sha256,
            "gates_id": inputs.gates_id,
            "gates_version": inputs.gates_version,
            "agent_name": inputs.agent_name,
            "agent_version": inputs.agent_version,
            "generated_at": inputs.generated_at,
        },
        "metrics": asdict(inputs.metrics),
        "gates": [
            {
                "metric": gate.metric,
                "status": gate.status.value,
                "bound": gate.bound,
                "observed": gate.observed,
                "rationale": gate.rationale,
            }
            for gate in inputs.gates
        ],
        "unassessed_metrics": list(inputs.unassessed),
        "cases": [_case(o) for o in inputs.outcomes],
        "limitations": list(inputs.limitations),
    }


def _fmt(value: float | None, *, percent: bool = False) -> str:
    if value is None:
        return "not measured"
    return f"{value * 100:.1f}%" if percent else f"{value:g}"


def render_markdown(inputs: ReportInputs) -> str:
    m = inputs.metrics
    lines = [
        "# Foundry agent lab evaluation",
        "",
        f"**Status: {inputs.status.upper()}** "
        f"({inputs.execution_mode} mode, {m.cases_run}/{m.cases_planned} cases)",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| dataset | `{inputs.dataset_id}` v{inputs.dataset_version} |",
        f"| dataset sha256 | `{inputs.dataset_sha256[:16]}…` |",
        f"| gates | `{inputs.gates_id}` v{inputs.gates_version} |",
        f"| agent | `{inputs.agent_name}` version `{inputs.agent_version}` |",
        f"| generated | {inputs.generated_at} |",
        "",
        "## Control metrics",
        "",
        "| Metric | Value | Denominator |",
        "| --- | --- | --- |",
        f"| unauthorised executions | {m.unauthorised_execution_count} | {m.cases_run} cases |",
        f"| approval compliance | {_fmt(m.approval_compliance_rate, percent=True)} "
        f"| {m.approval_compliance_cases} consequential cases |",
        f"| policy compliance | {_fmt(m.policy_compliance_rate, percent=True)} "
        f"| {m.policy_compliance_cases} cases |",
        f"| trajectory validity | {_fmt(m.trajectory_validity_rate, percent=True)} "
        f"| {m.cases_run} cases |",
        f"| task success | {_fmt(m.task_success_rate, percent=True)} "
        f"| {m.cases_completed} completed |",
        "",
        "## Model metrics",
        "",
        "In fake mode these come from scripted proposals and are reported, not gated.",
        "",
        "| Metric | Value | Denominator |",
        "| --- | --- | --- |",
        f"| tool selection accuracy | {_fmt(m.tool_selection_accuracy, percent=True)} "
        f"| {m.tool_selection_cases} tool-using cases |",
        f"| unnecessary tool calls | {_fmt(m.unnecessary_tool_call_rate, percent=True)} "
        f"| {m.unnecessary_tool_call_cases} no-tool cases |",
        "",
        "## Gates",
        "",
        "| Metric | Verdict | Bound | Observed |",
        "| --- | --- | --- | --- |",
    ]
    for gate in inputs.gates:
        lines.append(
            f"| `{gate.metric}` | {gate.status.value.upper()} | {gate.bound} "
            f"| {_fmt(gate.observed)} |"
        )
    if inputs.unassessed:
        lines += ["", "### Not assessed in this run", ""]
        lines += [f"- `{name}`" for name in inputs.unassessed]

    lines += [
        "",
        "## Cases",
        "",
        "| Case | Category | Expected | Observed | Trajectory |",
        "| --- | --- | --- | --- | --- |",
    ]
    for o in inputs.outcomes:
        trajectory = "valid" if o.trajectory_valid else ",".join(o.trajectory_defects) or "absent"
        lines.append(
            f"| `{o.case_id}` | {o.category} | {o.expected_outcome} "
            f"| {o.observed_outcome or 'raised'} | {trajectory} |"
        )
    lines += ["", "## What this report does not prove", ""]
    lines += [f"- {limitation}" for limitation in inputs.limitations]
    return "\n".join(lines)


def write_reports(inputs: ReportInputs, directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / JSON_NAME
    markdown_path = directory / MARKDOWN_NAME
    json_path.write_text(json.dumps(build_report(inputs), indent=2) + "\n")
    markdown_path.write_text(render_markdown(inputs) + "\n")
    return json_path, markdown_path


__all__ = [
    "JSON_NAME",
    "MARKDOWN_NAME",
    "REPORT_SCHEMA_VERSION",
    "ReportInputs",
    "build_report",
    "render_markdown",
    "write_reports",
]
