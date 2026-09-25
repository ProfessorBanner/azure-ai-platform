"""Reports: redaction, status, provenance and the dirty-tree rule."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from platform_engineering_assistant.domain import AnswerStatus, RefusalReason
from platform_engineering_assistant.errors import FailureCategory
from platform_engineering_assistant.evaluation.generation_metrics import compute_metrics
from platform_engineering_assistant.evaluation.generation_report import (
    JUDGE_LIMITATIONS,
    ReportInputs,
    build_report,
    render_markdown,
    write_reports,
)
from platform_engineering_assistant.evaluation.policy import (
    ExecutionMode,
    GateStatus,
    evaluate_gates,
    load_policy,
)
from platform_engineering_assistant.evaluation.provenance import RunProvenance
from tests.evaluation_fakes import outcome

SECRET_ANSWER = "Terraform state lives in Azure Blob Storage under platform/dev.tfstate."
SECRET_QUESTION = "Where does this platform keep its Terraform remote state?"


def provenance(**overrides: object) -> RunProvenance:
    defaults: dict[str, object] = {
        "git_sha": "a" * 40,
        "git_dirty": False,
        "dataset_id": "generation_v1",
        "dataset_version": 1,
        "dataset_sha256": "b" * 64,
        "corpus_version": 1,
        "retrieval_config_version": "retrieval_v1",
        "retrieval_config_sha256": "c" * 64,
        "prompt_version": "answer_v1",
        "prompt_sha256": "d" * 64,
        "judge_prompt_version": None,
        "judge_prompt_sha256": None,
        "provider": "fake-deterministic",
        "deployment": "fake-deterministic",
        "model": "fake-deterministic",
        "evaluation_policy_id": "evaluation_policy_v1",
        "evaluation_policy_version": 1,
        "evaluation_policy_sha256": "e" * 64,
        "generated_at": "2026-08-29T00:00:00+00:00",
        "case_count": 1,
        "execution_mode": "fake",
        "judge_enabled": False,
    }
    defaults.update(overrides)
    return RunProvenance(**defaults)  # type: ignore[arg-type]


def inputs(
    outcomes: list[object] | None = None,
    *,
    status: GateStatus = GateStatus.PASS,
    incomplete_reasons: tuple[str, ...] = (),
    **provenance_overrides: object,
) -> ReportInputs:
    cases = outcomes or [outcome()]
    metrics = compute_metrics(cases)  # type: ignore[arg-type]
    policy = load_policy()
    return ReportInputs(
        provenance=provenance(**provenance_overrides),
        metrics=metrics,
        gates=evaluate_gates(
            policy, metrics.gate_values(), ExecutionMode.FAKE, judge_enabled=False
        ),
        outcomes=cases,  # type: ignore[arg-type]
        status=status,
        operationally_complete=True,
        aborted_on=None,
        dataset_limitations=("not an independent benchmark",),
        incomplete_reasons=incomplete_reasons,
    )


# --- redaction is structural -------------------------------------------------


def test_a_case_outcome_has_no_field_for_content() -> None:
    """Redaction is not a step to remember; the fields do not exist."""
    import dataclasses

    from platform_engineering_assistant.evaluation.generation_metrics import CaseOutcome

    names = {field.name for field in dataclasses.fields(CaseOutcome)}
    for forbidden in ("question", "answer", "context", "text", "chunk_text", "rationale"):
        assert forbidden not in names


@pytest.mark.parametrize("secret", [SECRET_ANSWER, SECRET_QUESTION])
def test_no_answer_or_question_text_can_reach_a_report(secret: str) -> None:
    """There is no path: the runner never records it, so the report cannot print it."""
    rendered = json.dumps(build_report(inputs())) + render_markdown(inputs())
    assert secret not in rendered


def test_the_report_carries_identifiers_and_verdicts_only() -> None:
    entry = build_report(inputs())["cases"][0]
    assert set(entry) == {
        "case_id",
        "category",
        "tags",
        "expected_disposition",
        "observed_disposition",
        "expectation_met",
        "refusal_reason",
        "failure_category",
        "passed",
        "prohibited_hit",
        "citations_contained",
        "expected_doc_ids",
        "cited_doc_ids",
        "cited_chunk_ids",
        "matched_expected_docs",
        "answer_chars",
        "latency_ms",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "judge",
    }


# --- provenance --------------------------------------------------------------


@pytest.mark.parametrize(
    "field_name",
    [
        "git_sha",
        "git_dirty",
        "dataset_version",
        "dataset_sha256",
        "corpus_version",
        "retrieval_config_version",
        "retrieval_config_sha256",
        "prompt_version",
        "prompt_sha256",
        "judge_prompt_version",
        "judge_prompt_sha256",
        "provider",
        "deployment",
        "model",
        "evaluation_policy_version",
        "evaluation_policy_sha256",
        "generated_at",
        "case_count",
        "execution_mode",
    ],
)
def test_every_required_provenance_field_is_recorded(field_name: str) -> None:
    assert field_name in build_report(inputs())["provenance"]


def test_a_dirty_live_run_is_flagged_as_unattributable() -> None:
    record = provenance(execution_mode="live", git_dirty=True)
    assert record.unattributable_live_run


def test_an_unknown_sha_also_makes_a_live_run_unattributable() -> None:
    record = provenance(execution_mode="live", git_dirty=False, git_sha="unknown")
    assert record.unattributable_live_run


def test_a_dirty_offline_run_is_not_flagged() -> None:
    """Fake runs measure the working tree on purpose; that is what they are for."""
    assert not provenance(execution_mode="fake", git_dirty=True).unattributable_live_run


def test_a_dirty_live_report_says_so_prominently() -> None:
    rendered = render_markdown(
        inputs(
            status=GateStatus.INCOMPLETE,
            incomplete_reasons=("Live run from a dirty working tree.",),
            execution_mode="live",
            git_dirty=True,
        )
    )
    assert "cannot be attributed to a commit" in rendered
    assert "must never be adopted as a baseline" in rendered
    assert "DIRTY" in rendered


# --- status ------------------------------------------------------------------


@pytest.mark.parametrize("status", list(GateStatus))
def test_the_status_is_an_explicit_top_level_field(status: GateStatus) -> None:
    report = build_report(inputs(status=status))
    assert report["status"] == status.value
    assert f"**Status: {status.value.upper()}**" in render_markdown(inputs(status=status))


def test_incomplete_reasons_are_listed(tmp_path: Path) -> None:
    rendered = render_markdown(
        inputs(status=GateStatus.INCOMPLETE, incomplete_reasons=("the judge failed twice",))
    )
    assert "the judge failed twice" in rendered


# --- failures and limitations -------------------------------------------------


def test_individual_failures_are_listed_with_the_reason() -> None:
    outcomes = [
        outcome("good"),
        outcome("bad-content", prohibited_hit=True),
        outcome(
            "no-response",
            observed=None,
            expectation_met=False,
            failure=FailureCategory.TIMEOUT,
        ),
    ]
    rendered = render_markdown(inputs(outcomes))  # type: ignore[arg-type]
    assert "bad-content" in rendered
    assert "prohibited content" in rendered
    assert "no-response" in rendered
    assert "timeout" in rendered
    assert "`good`" not in rendered


def test_a_clean_run_says_so_rather_than_printing_an_empty_table() -> None:
    assert "None. Every case met its expectation." in render_markdown(inputs())


def test_containment_is_never_described_as_groundedness() -> None:
    """The most consequential wrong claim this codebase could make about itself."""
    report = build_report(inputs())
    deterministic = report["metrics"]["deterministic"]
    assert "citation_containment_rate" in deterministic
    assert "groundedness" not in deterministic

    rendered = render_markdown(inputs())
    assert "is **not** groundedness" in rendered


def test_the_gate_calibration_note_travels_with_every_report() -> None:
    note = " ".join(build_report(inputs())["limitations"]["gates"])
    assert "INITIAL ENGINEERING GATES" in note
    assert "recalibrated" in note


def test_judge_limitations_appear_only_when_the_judge_ran() -> None:
    without = build_report(inputs())["limitations"]["judge"]
    assert without == []

    with_judge = build_report(
        inputs(judge_enabled=True, judge_prompt_version="judge_v1", judge_prompt_sha256="f" * 64)
    )["limitations"]["judge"]
    assert list(JUDGE_LIMITATIONS) == with_judge
    assert any("correlated" in limitation for limitation in with_judge)


def test_dataset_limitations_are_carried_into_the_report() -> None:
    assert build_report(inputs())["limitations"]["dataset"] == ["not an independent benchmark"]


# --- writing ------------------------------------------------------------------


def test_both_reports_are_written_to_the_requested_directory(tmp_path: Path) -> None:
    json_path, markdown_path = write_reports(inputs(), tmp_path)
    assert json_path.parent == tmp_path
    assert json.loads(json_path.read_text())["schema_version"] == 1
    assert markdown_path.read_text().startswith("# Platform engineering assistant")


def test_the_default_destination_is_a_gitignored_artefacts_directory(repo_root: Path) -> None:
    """A report is run OUTPUT. Committing one makes the repository the place
    people read scores from rather than the place they reproduce them."""
    from platform_engineering_assistant.evaluation.generation_report import (
        DEFAULT_ARTIFACTS_DIR,
    )

    assert DEFAULT_ARTIFACTS_DIR.name == "evaluation"
    assert DEFAULT_ARTIFACTS_DIR.parent.name == "artifacts"
    assert "artifacts/" in (repo_root / ".gitignore").read_text()


def test_a_refusal_reason_is_reported_but_no_refusal_text_is() -> None:
    refusal = outcome(
        "r",
        expected=AnswerStatus.REFUSED,
        observed=AnswerStatus.REFUSED,
        cited=(),
        cited_docs=(),
        expected_docs=(),
    )
    entry = build_report(inputs([refusal]))["cases"][0]
    assert entry["refusal_reason"] is None
    assert entry["answer_chars"] == 0
    assert RefusalReason.OUT_OF_SCOPE.value not in json.dumps(entry)
