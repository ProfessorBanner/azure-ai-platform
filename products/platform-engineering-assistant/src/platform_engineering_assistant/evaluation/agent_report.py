"""The agent evaluation report: JSON for machines, Markdown for a reviewer.

WHAT A REPORT MAY CONTAIN
-------------------------
Identifiers, versions, hashes, enums, counts, rates and durations. Nothing else.
No question, no answer, no tool argument, no evidence text. The rule is enforced
structurally — `AgentCaseOutcome` has no such field — and the renderers below
therefore cannot leak one even by accident.

WHAT A REPORT MUST STATE ABOUT ITSELF
--------------------------------------
Three things, every time, because each is a way a reader could otherwise draw a
conclusion the run does not support:

  THE DATASET'S LIMITATIONS   carried verbatim from the set, so a passing report
                              cannot be read as a quality claim it never made.

  WHICH GATES DID NOT APPLY   a fake-mode run does not evaluate the model-side
                              gates at all. A report that showed ten passes
                              without saying five were never assessed would be
                              misleading by omission.

  WHY A CASE FAILED           per-case rows, so a failing rate can be traced to
                              a case rather than argued about as a number.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from platform_engineering_assistant.config import PRODUCT_ROOT
from platform_engineering_assistant.evaluation.agent_metrics import (
    AgentCaseOutcome,
    AgentMetrics,
)
from platform_engineering_assistant.evaluation.policy import GateResult, GateStatus

DEFAULT_AGENT_ARTIFACTS_DIR = PRODUCT_ROOT / "artifacts" / "agent-evaluation"

AGENT_REPORT_SCHEMA_VERSION = 1
JSON_REPORT_NAME = "agent-evaluation-report.json"
MARKDOWN_REPORT_NAME = "agent-evaluation-report.md"


@dataclass(frozen=True, slots=True)
class AgentRunProvenance:
    """Everything needed to say which artefacts produced these numbers.

    Its own type rather than the generation suite's, because an agent run has
    provenance the answering run does not: the agent prompt and its hash decide
    what proposals the model makes, and a report that named only the answering
    prompt could not distinguish two runs that differed in the agent prompt.
    """

    git_sha: str
    git_dirty: bool

    dataset_id: str
    dataset_version: int
    dataset_sha256: str

    corpus_version: int
    agent_prompt_version: str
    agent_prompt_sha256: str
    prompt_version: str
    retrieval_config_version: str

    provider: str
    deployment: str | None
    model: str | None

    policy_id: str
    policy_version: int
    policy_sha256: str

    generated_at: str
    case_count: int
    execution_mode: str

    @property
    def unattributable_live_run(self) -> bool:
        """A live run whose result cannot be tied to a commit, so never a baseline."""
        return self.execution_mode == "live" and (self.git_dirty or self.git_sha == "unknown")

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AgentReportInputs:
    """Everything a report is assembled from."""

    provenance: AgentRunProvenance
    metrics: AgentMetrics
    gates: list[GateResult]
    outcomes: list[AgentCaseOutcome]
    status: GateStatus
    operationally_complete: bool
    aborted_on: str | None
    dataset_limitations: tuple[str, ...]
    unassessed_metrics: tuple[str, ...] = ()
    incomplete_reasons: tuple[str, ...] = ()


def _case_entry(outcome: AgentCaseOutcome) -> dict[str, Any]:
    """One case, as identifiers and verdicts only."""
    return {
        "case_id": outcome.case_id,
        "category": outcome.category,
        "tags": list(outcome.tags),
        "expected_outcome": outcome.expected_outcome.value,
        "observed_outcome": (outcome.observed_outcome.value if outcome.observed_outcome else None),
        "task_successful": outcome.task_successful,
        "policy_compliant": outcome.policy_compliant,
        "approval_compliant": outcome.approval_compliant,
        "arguments_handled_correctly": outcome.arguments_handled_correctly,
        "trajectory_correct": outcome.trajectory_correct,
        "trajectory_defects": list(outcome.trajectory_defects),
        "unauthorised_execution": outcome.unauthorised_execution,
        "iteration_violation": outcome.iteration_violation,
        "tool_iterations": outcome.tool_iterations,
        "observed_tool": outcome.observed_tool,
        "execution_status": outcome.observed_execution_status.value,
        "citations_contained": outcome.citations_contained,
        "citation_count": outcome.citation_count,
        "prohibited_hit": outcome.prohibited_hit,
        "risk_claim_mismatch": outcome.risk_claim_mismatch,
        "failure_recovered": outcome.failure_recovered,
        "error_class": outcome.error_class,
        "latency_ms": round(outcome.latency_ms, 1),
        "total_tokens": outcome.total_tokens,
    }


def _metrics_payload(metrics: AgentMetrics) -> dict[str, Any]:
    payload = asdict(metrics)
    return {key: value for key, value in payload.items()}


def build_agent_report(inputs: AgentReportInputs) -> dict[str, Any]:
    """The machine-readable report."""
    return {
        "schema_version": AGENT_REPORT_SCHEMA_VERSION,
        "report_type": "agent_evaluation",
        "status": inputs.status.value,
        "operationally_complete": inputs.operationally_complete,
        "aborted_on": inputs.aborted_on,
        "incomplete_reasons": list(inputs.incomplete_reasons),
        "unassessed_metrics": list(inputs.unassessed_metrics),
        "provenance": inputs.provenance.as_dict(),
        "metrics": _metrics_payload(inputs.metrics),
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
        "cases": [_case_entry(outcome) for outcome in inputs.outcomes],
        "dataset_limitations": list(inputs.dataset_limitations),
    }


def _format(value: float | None, *, percent: bool = False) -> str:
    if value is None:
        return "not measured"
    if percent:
        return f"{value * 100:.1f}%"
    return f"{value:g}"


def render_agent_markdown(inputs: AgentReportInputs) -> str:
    """The reviewer-facing summary. Identifiers, versions, rates — never content."""
    provenance = inputs.provenance
    metrics = inputs.metrics
    lines: list[str] = [
        "# Agent evaluation report",
        "",
        f"**Status: {inputs.status.value.upper()}** "
        f"({provenance.execution_mode} mode, {metrics.cases_run}/{metrics.cases_planned} cases)",
        "",
    ]

    if not inputs.operationally_complete:
        lines += [
            f"> The run did not complete. Aborted on: {inputs.aborted_on or 'unknown'}.",
            "> Every rate below is computed over fewer cases than the set contains.",
            "",
        ]
    if provenance.unattributable_live_run:
        lines += [
            "> This live run cannot be attributed to a commit (dirty tree or unknown SHA).",
            "> It must not be used as a baseline.",
            "",
        ]

    lines += [
        "## Provenance",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| git sha | `{provenance.git_sha}`{' (dirty)' if provenance.git_dirty else ''} |",
        f"| dataset | `{provenance.dataset_id}` v{provenance.dataset_version} |",
        f"| dataset sha256 | `{provenance.dataset_sha256[:16]}…` |",
        f"| agent prompt | `{provenance.agent_prompt_version}` "
        f"(`{provenance.agent_prompt_sha256[:16]}…`) |",
        f"| answering prompt | `{provenance.prompt_version}` |",
        f"| retrieval config | `{provenance.retrieval_config_version}` |",
        f"| corpus version | {provenance.corpus_version} |",
        f"| provider | `{provenance.provider}` |",
        f"| model / deployment | `{provenance.model or 'n/a'}` / "
        f"`{provenance.deployment or 'n/a'}` |",
        f"| policy | `{provenance.policy_id}` v{provenance.policy_version} |",
        f"| generated | {provenance.generated_at} |",
        "",
        "## Control metrics",
        "",
        "These measure the APPLICATION's response to a proposal and are gated in both modes.",
        "",
        "| Metric | Value | Denominator |",
        "| --- | --- | --- |",
        f"| unauthorised executions | {metrics.unauthorised_execution_count} "
        f"| {metrics.cases_run} cases |",
        f"| approval compliance | {_format(metrics.approval_compliance_rate, percent=True)} "
        f"| {metrics.approval_compliance_cases} consequential cases |",
        f"| policy compliance | {_format(metrics.policy_compliance_rate, percent=True)} "
        f"| {metrics.policy_compliance_cases} cases |",
        f"| trajectory correctness | {_format(metrics.trajectory_correctness_rate, percent=True)} "
        f"| {metrics.cases_run} cases |",
        f"| argument correctness | {_format(metrics.argument_correctness_rate, percent=True)} "
        f"| {metrics.cases_run} cases |",
        f"| iteration violations | {metrics.max_iteration_violation_count} "
        f"| {metrics.cases_run} cases |",
        f"| tool failure recovery | {_format(metrics.tool_failure_recovery_rate, percent=True)} "
        f"| {metrics.tool_failure_recovery_cases} reliability cases |",
        f"| citation containment | {_format(metrics.citation_containment_rate, percent=True)} "
        f"| {metrics.citation_containment_cases} answered cases |",
        f"| grounding | {_format(metrics.grounding_rate, percent=True)} "
        f"| {metrics.grounding_cases} cases expecting citations |",
        f"| prohibited content | {_format(metrics.prohibited_content_rate, percent=True)} "
        f"| {metrics.cases_run} cases |",
        f"| risk claims contradicting the registry | {metrics.risk_claim_mismatch_count} "
        f"| {metrics.cases_run} cases |",
        "",
        "## Model metrics",
        "",
        "These measure the MODEL. In fake mode they are computed from scripted "
        "proposals and are reported but NOT gated.",
        "",
        "| Metric | Value | Denominator |",
        "| --- | --- | --- |",
        f"| task success | {_format(metrics.task_success_rate, percent=True)} "
        f"| {metrics.cases_completed} completed cases |",
        f"| tool selection accuracy | {_format(metrics.tool_selection_accuracy, percent=True)} "
        f"| {metrics.tool_selection_cases} tool-using cases |",
        f"| unnecessary tool calls | {_format(metrics.unnecessary_tool_call_rate, percent=True)} "
        f"| {metrics.unnecessary_tool_call_cases} no-tool cases |",
        f"| latency p95 | {_format(metrics.latency_p95_ms)} ms | {metrics.cases_completed} cases |",
        f"| total tokens p95 | {_format(metrics.total_tokens_p95)} | "
        f"{metrics.cases_completed} cases |",
        "",
        "## Gates",
        "",
        "| Metric | Verdict | Bound | Observed |",
        "| --- | --- | --- | --- |",
    ]

    for gate in inputs.gates:
        lines.append(
            f"| `{gate.metric}` | {gate.status.value.upper()} | {gate.bound} "
            f"| {_format(gate.observed)} |"
        )

    if inputs.unassessed_metrics:
        lines += [
            "",
            "### Not assessed in this run",
            "",
            "A metric absent from the gate table above was not evaluated, which is "
            "different from having passed:",
            "",
        ]
        lines += [f"- `{name}`" for name in inputs.unassessed_metrics]

    failing = [outcome for outcome in inputs.outcomes if not outcome.task_successful]
    lines += [
        "",
        "## Cases",
        "",
        "| Case | Category | Expected | Observed | Trajectory |",
        "| --- | --- | --- | --- | --- |",
    ]
    for outcome in inputs.outcomes:
        observed = outcome.observed_outcome.value if outcome.observed_outcome else "raised"
        trajectory = (
            "clean"
            if outcome.trajectory_correct
            else ",".join(outcome.trajectory_defects) or "absent"
        )
        lines.append(
            f"| `{outcome.case_id}` | {outcome.category} | {outcome.expected_outcome.value} "
            f"| {observed} | {trajectory} |"
        )

    if failing:
        lines += [
            "",
            f"{len(failing)} case(s) did not end as expected. In fake mode this is expected "
            "for cases whose correct outcome is a REFUSAL: the deterministic provider "
            "answers from the first chunk and cannot decide a disposition.",
        ]

    lines += ["", "## What this report does not prove", ""]
    lines += [f"- {limitation}" for limitation in inputs.dataset_limitations]
    return "\n".join(lines)


def write_agent_reports(
    inputs: AgentReportInputs, directory: Path | None = None
) -> tuple[Path, Path]:
    """Write both renderings. Returns their paths."""
    target = DEFAULT_AGENT_ARTIFACTS_DIR if directory is None else directory
    target.mkdir(parents=True, exist_ok=True)

    json_path = target / JSON_REPORT_NAME
    markdown_path = target / MARKDOWN_REPORT_NAME
    json_path.write_text(json.dumps(build_agent_report(inputs), indent=2) + "\n")
    markdown_path.write_text(render_agent_markdown(inputs) + "\n")
    return json_path, markdown_path


__all__ = [
    "DEFAULT_AGENT_ARTIFACTS_DIR",
    "JSON_REPORT_NAME",
    "MARKDOWN_REPORT_NAME",
    "AgentReportInputs",
    "AgentRunProvenance",
    "build_agent_report",
    "render_agent_markdown",
    "write_agent_reports",
]
