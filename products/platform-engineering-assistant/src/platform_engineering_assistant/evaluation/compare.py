"""Compare two evaluation reports and report what actually changed.

WHY NOT COMPARE THE HEADLINE NUMBERS
------------------------------------
Two runs can both report 87.5% disposition accuracy while disagreeing about
which two cases failed. Aggregates are lossy in exactly the direction that
matters: they hide the SUBSTITUTION of one failure for another, which is the
signature of a behaviour change rather than of noise. So this module diffs
CASES first and treats the aggregates as context.

It also diffs the things that explain a change — prompt, model, deployment,
retrieval configuration, dataset, policy — because "the score moved" and "the
prompt hash moved" appearing together is usually the whole investigation.

NO BASELINE IS SHIPPED
----------------------
There is deliberately no committed baseline report in this repository. The first
live report accepted after human review becomes the baseline, by a person
choosing it. Manufacturing a plausible-looking baseline would give every future
comparison a fictional reference point, and a comparison against fiction reads
exactly like a comparison against fact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from platform_engineering_assistant.errors import ConfigurationError
from platform_engineering_assistant.evaluation.policy import RegressionBounds

# Provenance fields whose change plausibly explains a behaviour change. Listed
# explicitly rather than diffing the whole record: a timestamp and a git SHA
# differ on every run and would bury the two or three fields that matter.
EXPLANATORY_FIELDS = (
    "prompt_version",
    "prompt_sha256",
    "judge_prompt_version",
    "judge_prompt_sha256",
    "model",
    "deployment",
    "provider",
    "retrieval_config_version",
    "retrieval_config_sha256",
    "corpus_version",
    "dataset_id",
    "dataset_version",
    "dataset_sha256",
    "evaluation_policy_version",
    "evaluation_policy_sha256",
    "execution_mode",
)


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One explanatory field that differs between the two reports."""

    field_name: str
    baseline: Any
    candidate: Any


@dataclass(frozen=True, slots=True)
class CaseChange:
    """One case that behaved differently."""

    case_id: str
    kind: str
    baseline: Any
    candidate: Any


@dataclass(frozen=True, slots=True)
class Comparison:
    """The full diff, grouped by the question it answers."""

    disposition_changes: list[CaseChange] = field(default_factory=list)
    newly_failed: list[CaseChange] = field(default_factory=list)
    newly_passed: list[CaseChange] = field(default_factory=list)
    newly_unsupported_claims: list[CaseChange] = field(default_factory=list)
    citation_document_changes: list[CaseChange] = field(default_factory=list)
    configuration_changes: list[FieldChange] = field(default_factory=list)
    latency_regression: str | None = None
    token_regression: str | None = None
    only_in_baseline: list[str] = field(default_factory=list)
    only_in_candidate: list[str] = field(default_factory=list)

    @property
    def has_regression(self) -> bool:
        """True when something got worse. A changed configuration is not, by
        itself, a regression — it is the explanation for one."""
        return bool(
            self.newly_failed
            or self.newly_unsupported_claims
            or self.latency_regression
            or self.token_regression
        )

    @property
    def has_changes(self) -> bool:
        return bool(
            self.has_regression
            or self.newly_passed
            or self.disposition_changes
            or self.citation_document_changes
            or self.configuration_changes
            or self.only_in_baseline
            or self.only_in_candidate
        )


def load_report(path: Path) -> dict[str, Any]:
    """Load a JSON evaluation report.

    Raises:
        ConfigurationError: if the file is missing, is not JSON, or is not a
            report this comparison understands.
    """
    try:
        raw = json.loads(path.read_bytes())
    except OSError as exc:
        raise ConfigurationError(f"Report could not be read: {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"{path.name} is not valid JSON: {exc.msg} (line {exc.lineno})"
        ) from exc

    if not isinstance(raw, dict) or "cases" not in raw or "provenance" not in raw:
        raise ConfigurationError(f"{path.name} is not an evaluation report.")
    return raw


def _cases_by_id(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cases = report.get("cases", [])
    return {case["case_id"]: case for case in cases if isinstance(case, dict)}


def _unsupported(case: dict[str, Any]) -> int | None:
    judge = case.get("judge")
    if not isinstance(judge, dict) or judge.get("judge_failed"):
        return None
    value = judge.get("claims_unsupported")
    return value if isinstance(value, int) else None


def _p95(report: dict[str, Any], section: str, key: str) -> float | None:
    metrics = report.get("metrics", {})
    value = metrics.get(section, {}).get(key) if isinstance(metrics, dict) else None
    return float(value) if isinstance(value, int | float) else None


def _regression_note(
    label: str,
    baseline: float | None,
    candidate: float | None,
    *,
    relative_bound: float,
    absolute_bound: float,
    unit: str,
) -> str | None:
    """Report a regression only when BOTH bounds are exceeded.

    Two bounds because either alone misreports at one end of the range: a
    relative bound calls a 2 ms increase on a 4 ms baseline a 50% regression,
    and an absolute bound stays silent while a slow run gets slower still.
    """
    if baseline is None or candidate is None or baseline <= 0:
        return None
    absolute = candidate - baseline
    relative = absolute / baseline
    if absolute <= absolute_bound or relative <= relative_bound:
        return None
    return (
        f"{label} rose from {baseline:,.1f}{unit} to {candidate:,.1f}{unit} "
        f"(+{absolute:,.1f}{unit}, +{relative:.1%})"
    )


def compare_reports(
    baseline: dict[str, Any], candidate: dict[str, Any], bounds: RegressionBounds
) -> Comparison:
    """Diff two reports, case by case, then by configuration, then by cost."""
    base_cases = _cases_by_id(baseline)
    cand_cases = _cases_by_id(candidate)
    shared = sorted(set(base_cases) & set(cand_cases))

    disposition_changes: list[CaseChange] = []
    newly_failed: list[CaseChange] = []
    newly_passed: list[CaseChange] = []
    newly_unsupported: list[CaseChange] = []
    citation_changes: list[CaseChange] = []

    for case_id in shared:
        before, after = base_cases[case_id], cand_cases[case_id]

        if before.get("observed_disposition") != after.get("observed_disposition"):
            disposition_changes.append(
                CaseChange(
                    case_id=case_id,
                    kind="disposition",
                    baseline=before.get("observed_disposition"),
                    candidate=after.get("observed_disposition"),
                )
            )

        if before.get("passed") and not after.get("passed"):
            newly_failed.append(
                CaseChange(case_id=case_id, kind="passed", baseline=True, candidate=False)
            )
        elif not before.get("passed") and after.get("passed"):
            newly_passed.append(
                CaseChange(case_id=case_id, kind="passed", baseline=False, candidate=True)
            )

        before_unsupported = _unsupported(before)
        after_unsupported = _unsupported(after)
        if (
            before_unsupported is not None
            and after_unsupported is not None
            and after_unsupported > before_unsupported
        ):
            newly_unsupported.append(
                CaseChange(
                    case_id=case_id,
                    kind="claims_unsupported",
                    baseline=before_unsupported,
                    candidate=after_unsupported,
                )
            )

        before_docs = sorted(before.get("cited_doc_ids") or [])
        after_docs = sorted(after.get("cited_doc_ids") or [])
        if before_docs != after_docs:
            citation_changes.append(
                CaseChange(
                    case_id=case_id,
                    kind="cited_doc_ids",
                    baseline=before_docs,
                    candidate=after_docs,
                )
            )

    base_provenance = baseline.get("provenance", {})
    cand_provenance = candidate.get("provenance", {})
    configuration_changes = [
        FieldChange(
            field_name=name,
            baseline=base_provenance.get(name),
            candidate=cand_provenance.get(name),
        )
        for name in EXPLANATORY_FIELDS
        if base_provenance.get(name) != cand_provenance.get(name)
    ]

    return Comparison(
        disposition_changes=disposition_changes,
        newly_failed=newly_failed,
        newly_passed=newly_passed,
        newly_unsupported_claims=newly_unsupported,
        citation_document_changes=citation_changes,
        configuration_changes=configuration_changes,
        latency_regression=_regression_note(
            "latency p95",
            _p95(baseline, "latency", "p95_ms"),
            _p95(candidate, "latency", "p95_ms"),
            relative_bound=bounds.latency_p95_relative_increase,
            absolute_bound=bounds.latency_p95_absolute_increase_ms,
            unit="ms",
        ),
        token_regression=_regression_note(
            "total tokens p95",
            _p95(baseline, "tokens", "total_p95"),
            _p95(candidate, "tokens", "total_p95"),
            relative_bound=bounds.total_tokens_p95_relative_increase,
            absolute_bound=bounds.total_tokens_p95_absolute_increase,
            unit="",
        ),
        only_in_baseline=sorted(set(base_cases) - set(cand_cases)),
        only_in_candidate=sorted(set(cand_cases) - set(base_cases)),
    )


def render_comparison(comparison: Comparison) -> str:
    """A human-readable diff. Identifiers and numbers only, as ever."""
    lines = ["# Evaluation report comparison", ""]

    verdict = (
        "REGRESSION"
        if comparison.has_regression
        else ("CHANGED" if comparison.has_changes else "NO CHANGE")
    )
    lines += [f"**Verdict: {verdict}**", ""]

    def section(title: str, changes: list[CaseChange]) -> None:
        # `lines.extend`, never `lines += `: an augmented assignment inside a
        # closure rebinds the name as a local and would shadow the list entirely.
        lines.extend([f"## {title}", ""])
        if not changes:
            lines.extend(["None.", ""])
            return
        lines.extend(["| Case | Baseline | Candidate |", "| --- | --- | --- |"])
        for change in changes:
            lines.append(f"| `{change.case_id}` | `{change.baseline}` | `{change.candidate}` |")
        lines.append("")

    section("Cases that changed disposition", comparison.disposition_changes)
    section("Newly failed cases", comparison.newly_failed)
    section("Newly passing cases", comparison.newly_passed)
    section("Newly unsupported claims", comparison.newly_unsupported_claims)
    section("Citation document changes", comparison.citation_document_changes)

    lines += ["## Cost regressions", ""]
    if comparison.latency_regression or comparison.token_regression:
        lines += [
            f"- {note}"
            for note in (comparison.latency_regression, comparison.token_regression)
            if note
        ]
    else:
        lines.append("None beyond the configured noise bounds.")
    lines.append("")

    lines += ["## Prompt / model / configuration changes", ""]
    if not comparison.configuration_changes:
        lines += ["None.", ""]
    else:
        lines += ["| Field | Baseline | Candidate |", "| --- | --- | --- |"]
        for change in comparison.configuration_changes:
            lines.append(f"| `{change.field_name}` | `{change.baseline}` | `{change.candidate}` |")
        lines.append("")

    if comparison.only_in_baseline or comparison.only_in_candidate:
        lines += [
            "## Case-set differences",
            "",
            f"- only in baseline: {', '.join(comparison.only_in_baseline) or 'none'}",
            f"- only in candidate: {', '.join(comparison.only_in_candidate) or 'none'}",
            "",
            "The two reports do not cover the same cases, so every aggregate below "
            "them is computed over different populations and must not be compared.",
            "",
        ]

    return "\n".join(lines)


__all__ = [
    "CaseChange",
    "Comparison",
    "FieldChange",
    "compare_reports",
    "load_report",
    "render_comparison",
]
