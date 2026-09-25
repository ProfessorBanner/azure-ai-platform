"""The commands, end to end, offline. Nothing here reaches Azure."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from platform_engineering_assistant.evaluation.generation_cli import (
    EXIT_CONFIGURATION,
    EXIT_FAIL,
    EXIT_PASS,
    build_parser,
    main,
)
from platform_engineering_assistant.evaluation.generation_report import (
    JSON_REPORT_NAME,
    MARKDOWN_REPORT_NAME,
)


@pytest.fixture(scope="module")
def offline_report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """One real offline run over the real corpus and the real dataset.

    Module-scoped because it builds the corpus index; every assertion below reads
    the same run, which is also the point — they describe one artefact.
    """
    directory = tmp_path_factory.mktemp("evaluation")
    assert main(["run", "--mode", "fake", "--output", str(directory)]) == EXIT_PASS
    payload: dict[str, object] = json.loads((directory / JSON_REPORT_NAME).read_text())
    return payload


# --- the offline command ------------------------------------------------------


def test_the_offline_run_passes_its_gates(offline_report: dict[str, object]) -> None:
    assert offline_report["status"] == "pass"
    assert offline_report["operationally_complete"] is True


def test_the_offline_run_covers_every_case(offline_report: dict[str, object]) -> None:
    cases = offline_report["cases"]
    assert isinstance(cases, list)
    assert len(cases) == 16
    assert all(case["failure_category"] is None for case in cases)


def test_the_offline_run_uses_the_deterministic_provider(
    offline_report: dict[str, object],
) -> None:
    provenance = offline_report["provenance"]
    assert isinstance(provenance, dict)
    assert provenance["provider"] == "fake-deterministic"
    assert provenance["execution_mode"] == "fake"
    assert provenance["judge_enabled"] is False


def test_only_the_structural_gates_run_offline(offline_report: dict[str, object]) -> None:
    """Quality gates are live-only: a deterministic stub cannot evidence quality."""
    gates = offline_report["gates"]
    assert isinstance(gates, list)
    assert {gate["metric"] for gate in gates} == {
        "citation_containment_rate",
        "prohibited_content_rate",
        "provider_failure_rate",
        "schema_validity_rate",
    }
    assert all(gate["status"] == "pass" for gate in gates)


def test_the_offline_run_proves_containment_holds_over_the_real_corpus(
    offline_report: dict[str, object],
) -> None:
    metrics = offline_report["metrics"]
    assert isinstance(metrics, dict)
    deterministic = metrics["deterministic"]
    assert deterministic["citation_containment_rate"] == 1.0
    assert deterministic["prohibited_content_rate"] == 0.0
    assert deterministic["schema_validity_rate"] == 1.0


def test_quality_metrics_are_still_computed_and_reported_offline(
    offline_report: dict[str, object],
) -> None:
    """Ungated is not unmeasured. The numbers are reported so they can be read."""
    metrics = offline_report["metrics"]
    assert isinstance(metrics, dict)
    deterministic = metrics["deterministic"]
    assert deterministic["disposition_accuracy"] is not None
    assert deterministic["expected_document_citation_recall"] is not None


def test_judge_metrics_are_absent_rather_than_zero_offline(
    offline_report: dict[str, object],
) -> None:
    metrics = offline_report["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["judge"]["semantic_groundedness"] is None
    assert metrics["judge"]["unsupported_claim_rate"] is None


def test_both_report_files_are_written(tmp_path: Path) -> None:
    assert main(["run", "--mode", "fake", "--output", str(tmp_path)]) == EXIT_PASS
    assert (tmp_path / JSON_REPORT_NAME).is_file()
    assert (tmp_path / MARKDOWN_REPORT_NAME).is_file()


def test_the_offline_run_is_deterministic(tmp_path: Path) -> None:
    """Same inputs, same verdicts. Only timing and provenance may move."""
    first, second = tmp_path / "a", tmp_path / "b"
    main(["run", "--mode", "fake", "--output", str(first)])
    main(["run", "--mode", "fake", "--output", str(second)])

    def verdicts(directory: Path) -> list[tuple[object, ...]]:
        payload = json.loads((directory / JSON_REPORT_NAME).read_text())
        return [
            (case["case_id"], case["observed_disposition"], case["passed"], case["cited_chunk_ids"])
            for case in payload["cases"]
        ]

    assert verdicts(first) == verdicts(second)


# --- the judge is live-only ---------------------------------------------------


def test_asking_for_the_judge_offline_is_refused_not_faked() -> None:
    """A stub cannot estimate semantic support, and a number that looked like one
    would be worse than no number at all."""
    assert main(["run", "--mode", "fake", "--judge"]) == EXIT_CONFIGURATION


# --- the comparison command ----------------------------------------------------


def test_comparing_a_report_with_itself_reports_no_regression(tmp_path: Path) -> None:
    main(["run", "--mode", "fake", "--output", str(tmp_path)])
    report = str(tmp_path / JSON_REPORT_NAME)
    assert main(["compare", report, report]) == EXIT_PASS


def test_comparing_against_a_regressed_report_exits_non_zero(tmp_path: Path) -> None:
    main(["run", "--mode", "fake", "--output", str(tmp_path)])
    baseline_path = tmp_path / JSON_REPORT_NAME
    payload = json.loads(baseline_path.read_text())

    regressed = json.loads(baseline_path.read_text())
    regressed["cases"][0]["passed"] = False
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(regressed))

    assert payload["cases"][0]["passed"] is True
    assert main(["compare", str(baseline_path), str(candidate_path)]) == EXIT_FAIL


def test_a_missing_report_is_a_configuration_error(tmp_path: Path) -> None:
    assert main(["compare", str(tmp_path / "a.json"), str(tmp_path / "b.json")]) == (
        EXIT_CONFIGURATION
    )


# --- the parser ----------------------------------------------------------------


def test_the_default_mode_is_the_free_one() -> None:
    """A command that costs money must be asked for explicitly."""
    arguments = build_parser().parse_args(["run"])
    assert arguments.mode == "fake"
    assert arguments.judge is False


def test_live_mode_must_be_named_explicitly() -> None:
    assert build_parser().parse_args(["run", "--mode", "live"]).mode == "live"


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
