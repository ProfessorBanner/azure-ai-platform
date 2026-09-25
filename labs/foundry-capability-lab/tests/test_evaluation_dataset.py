"""The shipped dataset, and the loader's refusal to accept a broken one."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundry_capability_lab.domain import RiskClassification, Severity
from foundry_capability_lab.errors import ConfigurationError
from foundry_capability_lab.evaluation.dataset import DEFAULT_DATASET_PATH, load_cases

MINIMUM_CASES = 10
MAXIMUM_CASES = 15


def test_shipped_dataset_loads() -> None:
    cases = load_cases()
    assert MINIMUM_CASES <= len(cases) <= MAXIMUM_CASES


def test_case_ids_are_unique() -> None:
    identifiers = [case.case_id for case in load_cases()]
    assert len(identifiers) == len(set(identifiers))


def test_dataset_covers_every_classification() -> None:
    """A dataset missing a class cannot detect a model that never predicts it."""
    covered = {case.expected_classification for case in load_cases()}
    assert covered == set(RiskClassification)


def test_dataset_covers_both_escalation_decisions() -> None:
    """Escalation accuracy is meaningless if every case has the same answer."""
    decisions = [case.expected_requires_escalation for case in load_cases()]
    assert True in decisions
    assert False in decisions
    # Roughly balanced, so 'always escalate' cannot score well.
    assert 0.3 <= sum(decisions) / len(decisions) <= 0.7


def test_dataset_spans_multiple_severities() -> None:
    assert len({case.expected_severity for case in load_cases()}) >= 4


def test_every_case_declares_evidence_present_in_its_observation() -> None:
    """Ground truth must be self-consistent or evidence scoring is nonsense."""
    for case in load_cases():
        for identifier in case.valid_evidence_ids:
            assert identifier in case.observation, (
                f"{case.case_id} lists evidence {identifier} absent from its observation"
            )


def test_missing_file_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        load_cases(Path("/nonexistent/cases.jsonl"))


def test_malformed_json_line_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text('{"case_id": "A", not json}\n')
    with pytest.raises(ConfigurationError) as caught:
        load_cases(path)
    assert "line 1" in str(caught.value)


def test_invalid_case_is_rejected_without_echoing_its_contents(tmp_path: Path) -> None:
    """Error text must not leak observation text."""
    secret = "confidential customer detail"
    path = tmp_path / "cases.jsonl"
    path.write_text(
        json.dumps(
            {
                "case_id": "A",
                "observation": secret,
                "expected_classification": "not_a_class",
                "expected_severity": "high",
                "expected_requires_escalation": True,
            }
        )
        + "\n"
    )
    with pytest.raises(ConfigurationError) as caught:
        load_cases(path)
    assert secret not in str(caught.value)


def test_empty_dataset_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text("\n\n")
    with pytest.raises(ConfigurationError):
        load_cases(path)


def test_duplicate_case_ids_are_rejected(tmp_path: Path) -> None:
    row = (
        '{"case_id": "DUP-1", "observation": "text", "expected_classification": "cost",'
        ' "expected_severity": "low", "expected_requires_escalation": false}'
    )
    path = tmp_path / "cases.jsonl"
    path.write_text(f"{row}\n{row}\n")
    with pytest.raises(ConfigurationError) as caught:
        load_cases(path)
    assert "DUP-1" in str(caught.value)


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        '{"case_id": "A", "observation": "t", "expected_classification": "cost",'
        ' "expected_severity": "low", "expected_requires_escalation": false,'
        ' "surprise": 1}\n'
    )
    with pytest.raises(ConfigurationError):
        load_cases(path)


def test_dataset_path_points_inside_the_lab() -> None:
    assert DEFAULT_DATASET_PATH.name == "risk_evaluation_cases.jsonl"
    assert DEFAULT_DATASET_PATH.exists()


def test_severity_and_classification_parse_to_enums() -> None:
    case = load_cases()[0]
    assert isinstance(case.expected_classification, RiskClassification)
    assert isinstance(case.expected_severity, Severity)
