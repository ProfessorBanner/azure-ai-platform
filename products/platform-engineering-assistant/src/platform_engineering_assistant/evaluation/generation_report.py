"""Report construction and rendering: JSON, Markdown, and per-case detail.

REDACTION IS STRUCTURAL
-----------------------
No question, answer, refusal text, chunk body, context block or judge rationale
reaches a report — because none of them is ever placed in a `CaseOutcome` in the
first place. There is no "remember to strip it" step for a later edit to forget.

What a report contains: identifiers, versions, hashes, enum values, counts,
durations and rates. All of it is safe to attach to a work item.

The QUESTIONS are not secret — they are committed in `evaluation/generation_v1.json`
and the report names the dataset and its hash, so anyone can look them up. The
MODEL'S RESPONSES are a different matter and are never written down: they are
generated content derived from the corpus, they are the most likely place for a
disclosed prompt or a fabricated credential to appear, and an evaluation artefact
is precisely the kind of file that gets pasted into a ticket.

THREE STATUSES
--------------
PASS, FAIL and INCOMPLETE are top-level fields, not something a reader infers
from the numbers. An aborted run, a run missing a required metric, and a live run
from a dirty tree all produce a report — the report is the diagnostic — but none
of them may read as a successful one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platform_engineering_assistant.config import PRODUCT_ROOT
from platform_engineering_assistant.evaluation.generation_metrics import (
    CaseOutcome,
    GenerationMetrics,
)
from platform_engineering_assistant.evaluation.policy import GateResult, GateStatus
from platform_engineering_assistant.evaluation.provenance import RunProvenance

DEFAULT_ARTIFACTS_DIR = PRODUCT_ROOT / "artifacts" / "evaluation"

REPORT_SCHEMA_VERSION = 1

JSON_REPORT_NAME = "evaluation-report.json"
MARKDOWN_REPORT_NAME = "evaluation-report.md"


@dataclass(frozen=True, slots=True)
class ReportInputs:
    """Everything a report is assembled from."""

    provenance: RunProvenance
    metrics: GenerationMetrics
    gates: list[GateResult]
    outcomes: list[CaseOutcome]
    status: GateStatus
    operationally_complete: bool
    aborted_on: str | None
    dataset_limitations: tuple[str, ...]
    incomplete_reasons: tuple[str, ...] = ()


def _case_entry(outcome: CaseOutcome) -> dict[str, Any]:
    """One case, as identifiers and verdicts only."""
    return {
        "case_id": outcome.case_id,
        "category": outcome.category,
        "tags": list(outcome.tags),
        "expected_disposition": outcome.expected_disposition.value,
        "observed_disposition": (
            outcome.observed_disposition.value if outcome.observed_disposition else None
        ),
        "expectation_met": outcome.expectation_met,
        "refusal_reason": outcome.refusal_reason.value if outcome.refusal_reason else None,
        "failure_category": (outcome.failure_category.value if outcome.failure_category else None),
        "passed": outcome.passed,
        "prohibited_hit": outcome.prohibited_hit,
        "citations_contained": outcome.citations_contained,
        "expected_doc_ids": list(outcome.expected_doc_ids),
        "cited_doc_ids": list(outcome.cited_doc_ids),
        "cited_chunk_ids": list(outcome.cited_chunk_ids),
        "matched_expected_docs": outcome.matched_expected_docs,
        "answer_chars": outcome.answer_chars,
        "latency_ms": round(outcome.latency_ms, 1),
        "input_tokens": outcome.input_tokens,
        "output_tokens": outcome.output_tokens,
        "total_tokens": outcome.total_tokens,
        "judge": (
            None
            if outcome.judge is None
            else {
                "claims_total": outcome.judge.claims_total,
                "claims_unsupported": outcome.judge.claims_unsupported,
                "answer_relevance": round(outcome.judge.answer_relevance, 4),
                "refusal_correct": outcome.judge.refusal_correct,
                "material_contradiction": outcome.judge.material_contradiction,
                "groundedness": round(outcome.judge.groundedness, 4),
                "judge_failed": outcome.judge.judge_failed,
            }
        ),
    }


def _metrics_payload(metrics: GenerationMetrics) -> dict[str, Any]:
    return {
        "cases": metrics.cases,
        "completed_cases": metrics.completed_cases,
        "deterministic": {
            "schema_validity_rate": metrics.schema_validity_rate,
            "disposition_accuracy": metrics.disposition_accuracy,
            "refusal_accuracy": metrics.refusal_accuracy,
            "answerable_case_accuracy": metrics.answerable_case_accuracy,
            # NOT groundedness: this proves only that citations came from
            # retrieved context. The name is load-bearing.
            "citation_containment_rate": metrics.citation_containment_rate,
            "expected_document_citation_recall": metrics.expected_document_citation_recall,
            "prohibited_content_rate": metrics.prohibited_content_rate,
            "provider_failure_rate": metrics.provider_failure_rate,
        },
        "latency": {
            "mean_ms": metrics.latency_mean_ms,
            "p95_ms": metrics.latency_p95_ms,
        },
        "tokens": {
            "input_mean": metrics.input_tokens_mean,
            "input_p95": metrics.input_tokens_p95,
            "output_mean": metrics.output_tokens_mean,
            "output_p95": metrics.output_tokens_p95,
            "total_mean": metrics.total_tokens_mean,
            "total_p95": metrics.total_tokens_p95,
        },
        "judge": {
            "judged_cases": metrics.judged_cases,
            "judge_failures": metrics.judge_failures,
            "semantic_groundedness": metrics.semantic_groundedness,
            "unsupported_claim_rate": metrics.unsupported_claim_rate,
            "answer_relevance": metrics.answer_relevance,
            "refusal_correctness": metrics.refusal_correctness,
        },
    }


JUDGE_LIMITATIONS = (
    "The judge and the generator run on the same deployment, so their errors are "
    "correlated and the bias runs towards leniency.",
    "LLM-judge scores are estimates produced by a model about a model. They are not "
    "ground truth and are not a correctness certificate.",
    "Human review remains necessary before acting on any answer in a consequential context.",
)

GATE_CALIBRATION_NOTE = (
    "These are INITIAL ENGINEERING GATES. They were chosen from the shape of a "
    "sixteen-case set written by the same engineering process that wrote the "
    "application, with no production traffic and no human-labelled data. They must "
    "be recalibrated against real traffic and human labels before a pass is treated "
    "as evidence of quality."
)


def build_report(inputs: ReportInputs) -> dict[str, Any]:
    """Assemble the machine-readable report."""
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": inputs.status.value,
        "operationally_complete": inputs.operationally_complete,
        "aborted_on": inputs.aborted_on,
        "incomplete_reasons": list(inputs.incomplete_reasons),
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
        "limitations": {
            "dataset": list(inputs.dataset_limitations),
            "gates": [GATE_CALIBRATION_NOTE],
            "judge": list(JUDGE_LIMITATIONS) if inputs.provenance.judge_enabled else [],
            "citation_containment": (
                "citation_containment_rate proves only that every citation named a "
                "chunk retrieved for that question. It is NOT groundedness and says "
                "nothing about whether the cited chunk supports the claim."
            ),
        },
    }


def _format_value(value: float | None, *, percent: bool) -> str:
    if value is None:
        return "not measured"
    return f"{value:.1%}" if percent else f"{value:,.1f}"


_PERCENT_METRICS = frozenset(
    {
        "schema_validity_rate",
        "disposition_accuracy",
        "refusal_accuracy",
        "answerable_case_accuracy",
        "citation_containment_rate",
        "expected_document_citation_recall",
        "prohibited_content_rate",
        "provider_failure_rate",
        "semantic_groundedness",
        "unsupported_claim_rate",
        "answer_relevance",
        "refusal_correctness",
    }
)


def render_markdown(inputs: ReportInputs) -> str:
    """A concise human summary. Contains no question, answer or evidence text."""
    provenance = inputs.provenance
    metrics = inputs.metrics

    lines: list[str] = [
        "# Platform engineering assistant — evaluation report",
        "",
        f"**Status: {inputs.status.value.upper()}**",
        "",
    ]

    if provenance.unattributable_live_run:
        lines += [
            "> **This live report cannot be attributed to a commit.** The working "
            "tree was dirty or the git SHA was unavailable, so this run is not "
            "reproducible and must never be adopted as a baseline.",
            "",
        ]

    if inputs.incomplete_reasons:
        lines.append("**Why this run is incomplete:**")
        lines += [f"- {reason}" for reason in inputs.incomplete_reasons]
        lines.append("")

    if not inputs.operationally_complete:
        lines += [
            f"> Run did not complete. Aborted on: `{inputs.aborted_on}`. Partial "
            "results below are a diagnostic, not a measurement.",
            "",
        ]

    lines += [
        "## Provenance",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| mode | `{provenance.execution_mode}` |",
        f"| git SHA | `{provenance.git_sha[:12]}` |",
        f"| working tree | {'DIRTY' if provenance.git_dirty else 'clean'} |",
        f"| dataset | `{provenance.dataset_id}` v{provenance.dataset_version} "
        f"(`{provenance.dataset_sha256[:12]}`) |",
        f"| corpus version | {provenance.corpus_version} |",
        f"| retrieval config | `{provenance.retrieval_config_version}` "
        f"(`{provenance.retrieval_config_sha256[:12]}`) |",
        f"| prompt | `{provenance.prompt_version}` (`{provenance.prompt_sha256[:12]}`) |",
        f"| judge prompt | `{provenance.judge_prompt_version or 'not used'}` "
        f"(`{(provenance.judge_prompt_sha256 or '')[:12]}`) |",
        f"| provider | `{provenance.provider}` |",
        f"| deployment / model | `{provenance.deployment}` / `{provenance.model}` |",
        f"| policy | `{provenance.evaluation_policy_id}` "
        f"v{provenance.evaluation_policy_version} "
        f"(`{provenance.evaluation_policy_sha256[:12]}`) |",
        f"| cases | {provenance.case_count} |",
        f"| generated | {provenance.generated_at} |",
        "",
        "## Gates",
        "",
        "| Metric | Bound | Observed | Result |",
        "| --- | --- | --- | --- |",
    ]

    for gate in inputs.gates:
        observed = _format_value(gate.observed, percent=gate.metric in _PERCENT_METRICS)
        lines.append(
            f"| `{gate.metric}` | {gate.bound} | {observed} | **{gate.status.value.upper()}** |"
        )

    lines += [
        "",
        f"_{GATE_CALIBRATION_NOTE}_",
        "",
        "## Aggregate metrics",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| cases / completed | {metrics.cases} / {metrics.completed_cases} |",
        f"| schema_validity_rate | {_format_value(metrics.schema_validity_rate, percent=True)} |",
        f"| disposition_accuracy | {_format_value(metrics.disposition_accuracy, percent=True)} |",
        f"| refusal_accuracy | {_format_value(metrics.refusal_accuracy, percent=True)} |",
        f"| answerable_case_accuracy | "
        f"{_format_value(metrics.answerable_case_accuracy, percent=True)} |",
        f"| citation_containment_rate | "
        f"{_format_value(metrics.citation_containment_rate, percent=True)} |",
        f"| expected_document_citation_recall | "
        f"{_format_value(metrics.expected_document_citation_recall, percent=True)} |",
        f"| prohibited_content_rate | "
        f"{_format_value(metrics.prohibited_content_rate, percent=True)} |",
        f"| provider_failure_rate | {_format_value(metrics.provider_failure_rate, percent=True)} |",
        "",
        "`citation_containment_rate` is **not** groundedness. It proves only that "
        "every citation named a chunk retrieved for that question.",
        "",
        "## Latency and tokens",
        "",
        "| Statistic | Mean | p95 |",
        "| --- | --- | --- |",
        f"| latency (ms) | {_format_value(metrics.latency_mean_ms, percent=False)} | "
        f"{_format_value(metrics.latency_p95_ms, percent=False)} |",
        f"| input tokens | {_format_value(metrics.input_tokens_mean, percent=False)} | "
        f"{_format_value(metrics.input_tokens_p95, percent=False)} |",
        f"| output tokens | {_format_value(metrics.output_tokens_mean, percent=False)} | "
        f"{_format_value(metrics.output_tokens_p95, percent=False)} |",
        f"| total tokens | {_format_value(metrics.total_tokens_mean, percent=False)} | "
        f"{_format_value(metrics.total_tokens_p95, percent=False)} |",
        "",
    ]

    if provenance.judge_enabled:
        lines += [
            "## Semantic judge",
            "",
            "| Metric | Value |",
            "| --- | --- |",
            f"| judged cases | {metrics.judged_cases} (failures: {metrics.judge_failures}) |",
            f"| semantic_groundedness | "
            f"{_format_value(metrics.semantic_groundedness, percent=True)} |",
            f"| unsupported_claim_rate | "
            f"{_format_value(metrics.unsupported_claim_rate, percent=True)} |",
            f"| answer_relevance | {_format_value(metrics.answer_relevance, percent=True)} |",
            f"| refusal_correctness | {_format_value(metrics.refusal_correctness, percent=True)} |",
            "",
            "**Judge limitations.**",
        ]
        lines += [f"- {limitation}" for limitation in JUDGE_LIMITATIONS]
        lines.append("")

    failures = [outcome for outcome in inputs.outcomes if not outcome.passed]
    lines += ["## Individual failures", ""]
    if not failures:
        lines.append("None. Every case met its expectation.")
    else:
        lines += [
            "| Case | Category | Expected | Observed | Why |",
            "| --- | --- | --- | --- | --- |",
        ]
        for outcome in failures:
            observed = (
                outcome.observed_disposition.value
                if outcome.observed_disposition
                else "no response"
            )
            reasons: list[str] = []
            if outcome.failure_category is not None:
                reasons.append(f"provider failure: `{outcome.failure_category.value}`")
            if outcome.prohibited_hit:
                reasons.append("prohibited content")
            if outcome.citations_contained is False:
                reasons.append("citation not retrieved")
            if outcome.completed and not outcome.expectation_met:
                reasons.append(
                    "unexpected disposition"
                    + (
                        f" (reason `{outcome.refusal_reason.value}`)"
                        if outcome.refusal_reason
                        else ""
                    )
                )
            lines.append(
                f"| `{outcome.case_id}` | {outcome.category} | "
                f"{outcome.expected_disposition.value} | {observed} | "
                f"{'; '.join(reasons) or 'did not pass'} |"
            )
    lines.append("")

    lines += ["## Dataset limitations", ""]
    lines += [f"- {limitation}" for limitation in inputs.dataset_limitations]
    lines.append("")

    return "\n".join(lines)


def write_reports(inputs: ReportInputs, directory: Path | None = None) -> tuple[Path, Path]:
    """Write both reports and return their paths.

    The default destination is a gitignored artefacts directory: a report is run
    OUTPUT, regenerated every time, and committing one would make the repository
    the place people read scores from rather than the place they reproduce them.
    """
    target = DEFAULT_ARTIFACTS_DIR if directory is None else directory
    target.mkdir(parents=True, exist_ok=True)

    json_path = target / JSON_REPORT_NAME
    markdown_path = target / MARKDOWN_REPORT_NAME

    json_path.write_text(json.dumps(build_report(inputs), indent=2, sort_keys=False) + "\n")
    markdown_path.write_text(render_markdown(inputs) + "\n")
    return json_path, markdown_path


__all__ = [
    "DEFAULT_ARTIFACTS_DIR",
    "GATE_CALIBRATION_NOTE",
    "JUDGE_LIMITATIONS",
    "ReportInputs",
    "build_report",
    "render_markdown",
    "write_reports",
]
