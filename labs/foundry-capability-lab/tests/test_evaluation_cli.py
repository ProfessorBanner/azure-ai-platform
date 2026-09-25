"""CLI exit-code contract. No test here reaches Azure.

The 0/1/2 split is the contract worth protecting: exit 1 means the harness
worked and the model missed a bar; exit 2 means no trustworthy measurement was
taken at all. A caller that cannot tell those apart cannot act on either.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundry_capability_lab.evaluation.cli import (
    EXIT_OPERATIONAL_FAILURE,
    EXIT_PASSED,
    EXIT_THRESHOLD_FAILED,
    main,
)

PASSING_THRESHOLDS = {
    "version": 1,
    "thresholds": {"schema_validity_rate": {"gated": True, "minimum": 0.5}},
}
IMPOSSIBLE_THRESHOLDS = {
    "version": 1,
    "thresholds": {"classification_accuracy": {"gated": True, "minimum": 1.0}},
}
UNGATED_IMPOSSIBLE = {
    "version": 1,
    "thresholds": {"classification_accuracy": {"gated": False, "minimum": 1.0}},
}


def write_thresholds(tmp_path: Path, payload: dict[str, object], name: str = "t.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


def offline_argv(tmp_path: Path, thresholds: Path, repetitions: int = 2) -> list[str]:
    return [
        "--provider",
        "offline",
        "--repetitions",
        str(repetitions),
        "--thresholds",
        str(thresholds),
        "--artifacts-dir",
        str(tmp_path / "artifacts"),
    ]


def test_passing_run_exits_zero(tmp_path: Path) -> None:
    thresholds = write_thresholds(tmp_path, PASSING_THRESHOLDS)
    assert main(offline_argv(tmp_path, thresholds)) == EXIT_PASSED


def test_failing_gate_exits_one(tmp_path: Path) -> None:
    """The evaluation completed; a gated threshold was not met."""
    thresholds = write_thresholds(tmp_path, IMPOSSIBLE_THRESHOLDS)
    assert main(offline_argv(tmp_path, thresholds)) == EXIT_THRESHOLD_FAILED


def test_ungated_breach_still_exits_zero(tmp_path: Path) -> None:
    thresholds = write_thresholds(tmp_path, UNGATED_IMPOSSIBLE)
    assert main(offline_argv(tmp_path, thresholds)) == EXIT_PASSED


def test_a_failing_run_still_writes_its_reports(tmp_path: Path) -> None:
    """Exit 1 is a result, so the evidence for it must be on disk."""
    thresholds = write_thresholds(tmp_path, IMPOSSIBLE_THRESHOLDS)
    main(offline_argv(tmp_path, thresholds))
    assert (tmp_path / "artifacts" / "evaluation-report.json").exists()
    assert (tmp_path / "artifacts" / "evaluation-report.md").exists()


def test_missing_dataset_exits_two(tmp_path: Path) -> None:
    thresholds = write_thresholds(tmp_path, PASSING_THRESHOLDS)
    argv = offline_argv(tmp_path, thresholds) + ["--dataset", str(tmp_path / "nope.jsonl")]
    assert main(argv) == EXIT_OPERATIONAL_FAILURE


def test_missing_thresholds_exits_two(tmp_path: Path) -> None:
    argv = offline_argv(tmp_path, tmp_path / "nope.json")
    assert main(argv) == EXIT_OPERATIONAL_FAILURE


def test_threshold_naming_an_unknown_metric_exits_two(tmp_path: Path) -> None:
    """A misconfigured gate is an operational fault, not a quality result."""
    thresholds = write_thresholds(
        tmp_path, {"version": 1, "thresholds": {"nonsense": {"gated": True, "minimum": 1.0}}}
    )
    assert main(offline_argv(tmp_path, thresholds)) == EXIT_OPERATIONAL_FAILURE


def test_zero_repetitions_exits_two(tmp_path: Path) -> None:
    thresholds = write_thresholds(tmp_path, PASSING_THRESHOLDS)
    assert main(offline_argv(tmp_path, thresholds, repetitions=0)) == EXIT_OPERATIONAL_FAILURE


def test_no_report_is_written_when_setup_fails(tmp_path: Path) -> None:
    """Setup failed, so there is nothing to report — as opposed to an aborted
    run, which keeps its partial diagnostic (covered below)."""
    argv = offline_argv(tmp_path, tmp_path / "nope.json")
    main(argv)
    assert not (tmp_path / "artifacts" / "evaluation-report.json").exists()


def test_default_repetitions_is_three() -> None:
    from foundry_capability_lab.evaluation.cli import build_parser

    assert build_parser().parse_args([]).repetitions == 3


def test_default_provider_is_the_real_one() -> None:
    """Offline must be opt-in: a default fake would silently fake a capability run."""
    from foundry_capability_lab.evaluation.cli import build_parser

    assert build_parser().parse_args([]).provider == "foundry"


def test_offline_run_labels_itself_in_the_report(tmp_path: Path) -> None:
    """An offline run must never be mistakable for a real capability result."""
    thresholds = write_thresholds(tmp_path, PASSING_THRESHOLDS)
    main(offline_argv(tmp_path, thresholds))
    report = json.loads((tmp_path / "artifacts" / "evaluation-report.json").read_text())
    assert report["run"]["provider"] == "offline-deterministic"


def test_reports_never_contain_dataset_observation_text(tmp_path: Path) -> None:
    """End-to-end privacy check against the REAL shipped dataset."""
    from foundry_capability_lab.evaluation.dataset import load_cases

    thresholds = write_thresholds(tmp_path, PASSING_THRESHOLDS)
    main(offline_argv(tmp_path, thresholds))

    written = (tmp_path / "artifacts" / "evaluation-report.json").read_text()
    written += (tmp_path / "artifacts" / "evaluation-report.md").read_text()

    for case in load_cases():
        # A distinctive fragment of each observation must be absent.
        fragment = case.observation[:40]
        assert fragment not in written, f"{case.case_id} observation text leaked into a report"
        assert case.notes not in written or not case.notes


def test_summary_is_printed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    thresholds = write_thresholds(tmp_path, PASSING_THRESHOLDS)
    main(offline_argv(tmp_path, thresholds))
    out = capsys.readouterr().out
    assert "Evaluation PASSED" in out
    assert "attempts" in out


# --- pacing (Phase 16.3A) ----------------------------------------------------


def test_live_run_requires_an_explicit_pacing_delay(tmp_path: Path) -> None:
    """The harness must not assume a deployment's rate limit on the operator's behalf."""
    thresholds = write_thresholds(tmp_path, PASSING_THRESHOLDS)
    exit_code = main(
        [
            "--provider",
            "foundry",
            "--thresholds",
            str(thresholds),
            "--artifacts-dir",
            str(tmp_path / "artifacts"),
        ]
    )
    assert exit_code == EXIT_OPERATIONAL_FAILURE


def test_live_run_rejects_a_zero_delay(tmp_path: Path) -> None:
    thresholds = write_thresholds(tmp_path, PASSING_THRESHOLDS)
    exit_code = main(
        [
            "--provider",
            "foundry",
            "--inter-attempt-delay-seconds",
            "0",
            "--thresholds",
            str(thresholds),
            "--artifacts-dir",
            str(tmp_path / "artifacts"),
        ]
    )
    assert exit_code == EXIT_OPERATIONAL_FAILURE


@pytest.mark.parametrize("provider", ["offline", "foundry"])
def test_negative_delay_is_rejected_for_any_provider(tmp_path: Path, provider: str) -> None:
    thresholds = write_thresholds(tmp_path, PASSING_THRESHOLDS)
    exit_code = main(
        [
            "--provider",
            provider,
            "--inter-attempt-delay-seconds",
            "-2",
            "--thresholds",
            str(thresholds),
            "--artifacts-dir",
            str(tmp_path / "artifacts"),
        ]
    )
    assert exit_code == EXIT_OPERATIONAL_FAILURE


def test_pacing_is_rejected_before_any_provider_is_constructed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing delay must cost no credential, no client and no call."""

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("provider was constructed despite invalid pacing")

    monkeypatch.setattr(
        "foundry_capability_lab.evaluation.cli.FoundryRiskAssessmentProvider.from_config",
        explode,
    )

    thresholds = write_thresholds(tmp_path, PASSING_THRESHOLDS)
    assert (
        main(
            [
                "--provider",
                "foundry",
                "--thresholds",
                str(thresholds),
                "--artifacts-dir",
                str(tmp_path / "artifacts"),
            ]
        )
        == EXIT_OPERATIONAL_FAILURE
    )


def test_invalid_endpoint_is_rejected_before_any_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generic account endpoint must fail at configuration, not at 404."""

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("provider was constructed despite an invalid endpoint")

    monkeypatch.setattr(
        "foundry_capability_lab.evaluation.cli.FoundryRiskAssessmentProvider.from_config",
        explode,
    )
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.cognitiveservices.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4-1-mini")

    thresholds = write_thresholds(tmp_path, PASSING_THRESHOLDS)
    assert (
        main(
            [
                "--provider",
                "foundry",
                "--inter-attempt-delay-seconds",
                "6",
                "--thresholds",
                str(thresholds),
                "--artifacts-dir",
                str(tmp_path / "artifacts"),
            ]
        )
        == EXIT_OPERATIONAL_FAILURE
    )


def test_offline_run_performs_no_real_sleeping(tmp_path: Path) -> None:
    """An offline run defaults to zero delay, so the suite never waits."""
    import time as time_module

    thresholds = write_thresholds(tmp_path, PASSING_THRESHOLDS)
    started = time_module.perf_counter()
    assert main(offline_argv(tmp_path, thresholds)) == EXIT_PASSED
    assert time_module.perf_counter() - started < 5.0

    report = json.loads((tmp_path / "artifacts" / "evaluation-report.json").read_text())
    assert report["run"]["inter_attempt_delay_seconds"] == 0.0


# --- operational completeness and exit codes --------------------------------


def test_aborted_run_exits_two_and_keeps_a_partial_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rate limiting stops the run; the partial record is kept as a diagnostic."""
    import foundry_capability_lab.evaluation.cli as cli_module
    from foundry_capability_lab.errors import RateLimitedError
    from tests.evaluation_fakes import AlwaysFailingProvider

    monkeypatch.setattr(
        cli_module,
        "OfflineRiskAssessmentProvider",
        lambda *a, **k: AlwaysFailingProvider(RateLimitedError("429")),
    )

    thresholds = write_thresholds(tmp_path, PASSING_THRESHOLDS)
    assert main(offline_argv(tmp_path, thresholds)) == EXIT_OPERATIONAL_FAILURE

    report = json.loads((tmp_path / "artifacts" / "evaluation-report.json").read_text())
    assert report["operationally_complete"] is False
    assert report["aborted_on"] == "rate_limited"
    assert report["run"]["attempts"] < report["run"]["planned_attempts"]


def test_an_incomplete_run_exits_two_even_if_partial_gates_would_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partial numbers are not a verdict, however flattering they look."""
    import foundry_capability_lab.evaluation.cli as cli_module
    from foundry_capability_lab.errors import AuthorizationError
    from tests.evaluation_fakes import AlwaysFailingProvider

    monkeypatch.setattr(
        cli_module,
        "OfflineRiskAssessmentProvider",
        lambda *a, **k: AlwaysFailingProvider(AuthorizationError("403")),
    )
    # A threshold that anything would satisfy.
    thresholds = write_thresholds(
        tmp_path,
        {"version": 1, "thresholds": {"provider_error_rate": {"gated": True, "maximum": 1.0}}},
    )
    assert main(offline_argv(tmp_path, thresholds)) == EXIT_OPERATIONAL_FAILURE


def test_schema_invalid_responses_complete_the_run_and_can_exit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed response is quality evidence, not an operational fault."""
    import foundry_capability_lab.evaluation.cli as cli_module
    from foundry_capability_lab.errors import InvalidStructuredOutputError
    from tests.evaluation_fakes import AlwaysFailingProvider

    monkeypatch.setattr(
        cli_module,
        "OfflineRiskAssessmentProvider",
        lambda *a, **k: AlwaysFailingProvider(InvalidStructuredOutputError("bad")),
    )
    thresholds = write_thresholds(
        tmp_path,
        {"version": 1, "thresholds": {"schema_validity_rate": {"gated": True, "minimum": 1.0}}},
    )
    assert main(offline_argv(tmp_path, thresholds)) == EXIT_THRESHOLD_FAILED

    report = json.loads((tmp_path / "artifacts" / "evaluation-report.json").read_text())
    assert report["operationally_complete"] is True


def test_complete_run_marks_itself_complete(tmp_path: Path) -> None:
    thresholds = write_thresholds(tmp_path, PASSING_THRESHOLDS)
    main(offline_argv(tmp_path, thresholds))
    report = json.loads((tmp_path / "artifacts" / "evaluation-report.json").read_text())
    assert report["operationally_complete"] is True
    assert report["aborted_on"] is None
    assert report["run"]["attempts"] == report["run"]["planned_attempts"]


def test_no_retry_occurs_across_a_full_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Calls must equal cases x repetitions exactly — never more."""
    import foundry_capability_lab.evaluation.cli as cli_module
    from tests.evaluation_fakes import ConstantProvider

    counter = ConstantProvider()
    monkeypatch.setattr(cli_module, "OfflineRiskAssessmentProvider", lambda *a, **k: counter)

    thresholds = write_thresholds(tmp_path, PASSING_THRESHOLDS)
    main(offline_argv(tmp_path, thresholds, repetitions=2))

    report = json.loads((tmp_path / "artifacts" / "evaluation-report.json").read_text())
    assert counter.calls == report["run"]["attempts"]
    assert report["run"]["retries"] == 0


def test_aborted_report_is_still_structurally_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The partial diagnostic must leak no observation text or rationale."""
    import foundry_capability_lab.evaluation.cli as cli_module
    from foundry_capability_lab.errors import RateLimitedError
    from foundry_capability_lab.evaluation.dataset import load_cases
    from tests.evaluation_fakes import AlwaysFailingProvider

    monkeypatch.setattr(
        cli_module,
        "OfflineRiskAssessmentProvider",
        lambda *a, **k: AlwaysFailingProvider(RateLimitedError("429")),
    )
    thresholds = write_thresholds(tmp_path, PASSING_THRESHOLDS)
    main(offline_argv(tmp_path, thresholds))

    written = (tmp_path / "artifacts" / "evaluation-report.json").read_text()
    written += (tmp_path / "artifacts" / "evaluation-report.md").read_text()
    for case in load_cases():
        assert case.observation[:40] not in written
