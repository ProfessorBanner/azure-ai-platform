"""The fixed evaluation dataset and its loader.

The dataset is a version-controlled JSONL file, not generated and not fetched.
A moving dataset makes a moving metric: if the cases can change underneath a
run, comparing today's score with last week's is meaningless. Changing a case is
therefore a reviewable diff.

Each case carries the ground truth needed to score a response WITHOUT a second
model in the loop: an expected classification, an expected severity, an expected
escalation decision, and the exact set of evidence identifiers that legitimately
appear in the observation text.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from foundry_capability_lab.domain import RiskClassification, Severity
from foundry_capability_lab.errors import ConfigurationError

# Packaged alongside the source so a run needs no external path.
DEFAULT_DATASET_PATH = Path(__file__).resolve().parents[3] / "data" / "risk_evaluation_cases.jsonl"


class EvaluationCase(BaseModel):
    """One labelled observation.

    `observation` is the only free text here, and it is an INPUT: it is never
    copied into a report. Reports identify a case by `case_id` alone.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1)
    observation: str = Field(min_length=1)
    expected_classification: RiskClassification
    expected_severity: Severity
    expected_requires_escalation: bool
    valid_evidence_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Identifiers that genuinely appear in the observation. Any evidence id "
            "outside this set is fabricated, which is what the evidence-validity "
            "metric detects."
        ),
    )
    notes: str = Field(
        default="",
        description="Why this case is labelled the way it is. For humans; never reported.",
    )


def load_cases(path: Path | None = None) -> list[EvaluationCase]:
    """Load and validate the dataset.

    Raises:
        ConfigurationError: if the file is missing, malformed, empty, or contains
            duplicate case ids. All of these are operational failures — the run
            cannot produce a trustworthy metric — so they are surfaced as
            configuration errors rather than being tolerated.
    """
    dataset_path = DEFAULT_DATASET_PATH if path is None else path

    try:
        raw_text = dataset_path.read_text()
    except OSError as exc:
        raise ConfigurationError(f"Evaluation dataset could not be read: {dataset_path}") from exc

    cases: list[EvaluationCase] = []
    for number, line in enumerate(raw_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                f"{dataset_path.name} line {number} is not valid JSON: {exc.msg}"
            ) from exc
        try:
            cases.append(EvaluationCase.model_validate(payload))
        except Exception as exc:
            # The message deliberately reports the line, not the payload: a case
            # body is input text and does not belong in an error string.
            raise ConfigurationError(
                f"{dataset_path.name} line {number} is not a valid evaluation case."
            ) from exc

    if not cases:
        raise ConfigurationError(f"Evaluation dataset {dataset_path.name} contains no cases.")

    identifiers = [case.case_id for case in cases]
    duplicates = sorted({name for name in identifiers if identifiers.count(name) > 1})
    if duplicates:
        raise ConfigurationError(
            f"Evaluation dataset contains duplicate case ids: {', '.join(duplicates)}"
        )

    return cases
