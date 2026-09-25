"""Report comparison: per-case diffs first, aggregates never on their own."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from platform_engineering_assistant.errors import ConfigurationError
from platform_engineering_assistant.evaluation.compare import (
    compare_reports,
    load_report,
    render_comparison,
)
from platform_engineering_assistant.evaluation.policy import load_policy

BOUNDS = load_policy().regression


def case_entry(
    case_id: str = "C-1",
    *,
    observed: str | None = "answered",
    passed: bool = True,
    cited_doc_ids: list[str] | None = None,
    claims_unsupported: int | None = None,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "observed_disposition": observed,
        "passed": passed,
        "cited_doc_ids": cited_doc_ids if cited_doc_ids is not None else ["adr-0001"],
        "judge": (
            None
            if claims_unsupported is None
            else {"claims_unsupported": claims_unsupported, "judge_failed": False}
        ),
    }


def report(
    cases: list[dict[str, Any]] | None = None,
    *,
    latency_p95: float = 100.0,
    tokens_p95: float = 1000.0,
    **provenance: Any,
) -> dict[str, Any]:
    base = {
        "prompt_version": "answer_v1",
        "prompt_sha256": "a" * 64,
        "model": "gpt-4-1-mini",
        "deployment": "dep",
        "provider": "azure-openai",
        "retrieval_config_version": "retrieval_v1",
        "retrieval_config_sha256": "b" * 64,
        "corpus_version": 1,
        "dataset_id": "generation_v1",
        "dataset_version": 1,
        "dataset_sha256": "c" * 64,
        "evaluation_policy_version": 1,
        "evaluation_policy_sha256": "d" * 64,
        "execution_mode": "live",
    }
    base.update(provenance)
    return {
        "provenance": base,
        "metrics": {"latency": {"p95_ms": latency_p95}, "tokens": {"total_p95": tokens_p95}},
        "cases": cases if cases is not None else [case_entry()],
    }


# --- per-case diffs ----------------------------------------------------------


def test_identical_reports_show_no_change() -> None:
    comparison = compare_reports(report(), report(), BOUNDS)
    assert not comparison.has_changes
    assert not comparison.has_regression
    assert "NO CHANGE" in render_comparison(comparison)


def test_a_changed_disposition_is_identified() -> None:
    comparison = compare_reports(
        report([case_entry(observed="answered")]),
        report([case_entry(observed="refused", passed=False)]),
        BOUNDS,
    )
    assert [change.case_id for change in comparison.disposition_changes] == ["C-1"]
    assert comparison.disposition_changes[0].baseline == "answered"
    assert comparison.disposition_changes[0].candidate == "refused"


def test_a_newly_failed_case_is_identified() -> None:
    comparison = compare_reports(
        report([case_entry(passed=True)]),
        report([case_entry(passed=False)]),
        BOUNDS,
    )
    assert [change.case_id for change in comparison.newly_failed] == ["C-1"]
    assert comparison.has_regression


def test_a_newly_passing_case_is_reported_but_is_not_a_regression() -> None:
    comparison = compare_reports(
        report([case_entry(passed=False)]),
        report([case_entry(passed=True)]),
        BOUNDS,
    )
    assert [change.case_id for change in comparison.newly_passed] == ["C-1"]
    assert not comparison.has_regression
    assert comparison.has_changes


def test_substituted_failures_are_visible_even_though_the_totals_match() -> None:
    """The reason aggregates are never compared on their own.

    Both runs fail exactly one case, so every headline rate is identical while
    the behaviour has plainly changed.
    """
    baseline = report([case_entry("A", passed=True), case_entry("B", passed=False)])
    candidate = report([case_entry("A", passed=False), case_entry("B", passed=True)])
    comparison = compare_reports(baseline, candidate, BOUNDS)

    assert [change.case_id for change in comparison.newly_failed] == ["A"]
    assert [change.case_id for change in comparison.newly_passed] == ["B"]
    assert comparison.has_regression


def test_newly_unsupported_claims_are_identified() -> None:
    comparison = compare_reports(
        report([case_entry(claims_unsupported=1)]),
        report([case_entry(claims_unsupported=4)]),
        BOUNDS,
    )
    assert comparison.newly_unsupported_claims[0].baseline == 1
    assert comparison.newly_unsupported_claims[0].candidate == 4
    assert comparison.has_regression


def test_fewer_unsupported_claims_is_not_a_regression() -> None:
    comparison = compare_reports(
        report([case_entry(claims_unsupported=4)]),
        report([case_entry(claims_unsupported=1)]),
        BOUNDS,
    )
    assert not comparison.newly_unsupported_claims


def test_a_failed_judgement_is_not_read_as_zero_unsupported_claims() -> None:
    baseline = report([case_entry(claims_unsupported=3)])
    candidate = report([case_entry(claims_unsupported=0)])
    candidate["cases"][0]["judge"]["judge_failed"] = True
    assert not compare_reports(baseline, candidate, BOUNDS).newly_unsupported_claims


def test_citation_document_changes_are_identified() -> None:
    comparison = compare_reports(
        report([case_entry(cited_doc_ids=["adr-0001"])]),
        report([case_entry(cited_doc_ids=["adr-0003"])]),
        BOUNDS,
    )
    assert comparison.citation_document_changes[0].baseline == ["adr-0001"]
    assert comparison.citation_document_changes[0].candidate == ["adr-0003"]


def test_citation_order_alone_is_not_a_change() -> None:
    comparison = compare_reports(
        report([case_entry(cited_doc_ids=["adr-0001", "adr-0003"])]),
        report([case_entry(cited_doc_ids=["adr-0003", "adr-0001"])]),
        BOUNDS,
    )
    assert not comparison.citation_document_changes


# --- cost regressions ---------------------------------------------------------


def test_a_latency_regression_needs_both_a_relative_and_an_absolute_breach() -> None:
    """A relative bound alone calls 2 ms on a 4 ms baseline a 50% regression."""
    small = compare_reports(report(latency_p95=4.0), report(latency_p95=6.0), BOUNDS)
    assert small.latency_regression is None

    real = compare_reports(report(latency_p95=2000.0), report(latency_p95=4000.0), BOUNDS)
    assert real.latency_regression is not None
    assert "+100.0%" in real.latency_regression


def test_a_large_absolute_but_small_relative_change_is_not_a_regression() -> None:
    comparison = compare_reports(report(latency_p95=100000.0), report(latency_p95=101000.0), BOUNDS)
    assert comparison.latency_regression is None


def test_a_token_regression_is_identified() -> None:
    comparison = compare_reports(report(tokens_p95=1000.0), report(tokens_p95=2000.0), BOUNDS)
    assert comparison.token_regression is not None
    assert comparison.has_regression


def test_an_improvement_is_never_a_regression() -> None:
    comparison = compare_reports(
        report(latency_p95=4000.0, tokens_p95=2000.0),
        report(latency_p95=1000.0, tokens_p95=800.0),
        BOUNDS,
    )
    assert comparison.latency_regression is None
    assert comparison.token_regression is None


def test_a_missing_statistic_is_not_treated_as_zero() -> None:
    candidate = report()
    candidate["metrics"]["latency"] = {}
    assert compare_reports(report(), candidate, BOUNDS).latency_regression is None


# --- configuration changes ----------------------------------------------------


def test_prompt_model_and_configuration_changes_are_reported() -> None:
    comparison = compare_reports(
        report(),
        report(prompt_sha256="z" * 64, model="gpt-5", retrieval_config_version="retrieval_v2"),
        BOUNDS,
    )
    changed = {change.field_name for change in comparison.configuration_changes}
    assert changed == {"prompt_sha256", "model", "retrieval_config_version"}


def test_a_configuration_change_alone_is_not_a_regression() -> None:
    """It is the explanation for one, not the thing itself."""
    comparison = compare_reports(report(), report(model="gpt-5"), BOUNDS)
    assert comparison.has_changes
    assert not comparison.has_regression


def test_a_timestamp_difference_is_not_reported_as_a_change() -> None:
    comparison = compare_reports(
        report(generated_at="2026-01-01T00:00:00+00:00"),
        report(generated_at="2026-02-02T00:00:00+00:00"),
        BOUNDS,
    )
    assert not comparison.configuration_changes


# --- differing case sets ------------------------------------------------------


def test_differing_case_sets_are_called_out() -> None:
    comparison = compare_reports(
        report([case_entry("A"), case_entry("B")]), report([case_entry("A")]), BOUNDS
    )
    assert comparison.only_in_baseline == ["B"]
    assert "must not be compared" in render_comparison(comparison)


# --- loading ------------------------------------------------------------------


def test_a_non_report_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "not-a-report.json"
    path.write_text(json.dumps({"hello": "world"}))
    with pytest.raises(ConfigurationError, match="not an evaluation report"):
        load_report(path)


def test_a_missing_report_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="could not be read"):
        load_report(tmp_path / "absent.json")


def test_the_rendering_names_every_diff_category() -> None:
    rendered = render_comparison(compare_reports(report(), report(), BOUNDS))
    for heading in (
        "changed disposition",
        "Newly failed cases",
        "Newly unsupported claims",
        "Citation document changes",
        "Cost regressions",
        "configuration changes",
    ):
        assert heading in rendered
