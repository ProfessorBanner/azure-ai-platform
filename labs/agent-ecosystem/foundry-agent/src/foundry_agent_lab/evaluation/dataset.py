"""The versioned lab evaluation set: cases, scripted proposals, expectations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from foundry_agent_lab._product import repository_root

DATASET_PATH = repository_root() / "labs/agent-ecosystem/foundry-agent/evaluation/agent_lab_v1.json"
GATES_PATH = repository_root() / "labs/agent-ecosystem/foundry-agent/evaluation/gates_v1.json"

REQUIRED_CATEGORIES = {
    "direct-answer": 1,
    "read-only-tool": 1,
    "unnecessary-tool": 1,
    "approval-required": 1,
    "adversarial": 1,
}


class DatasetError(Exception):
    """The set could not be loaded or does not hold its promised shape."""


class ScriptedCall(BaseModel):
    """One function call the managed runtime is taken to have proposed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    arguments: dict[str, str] = Field(default_factory=dict)


class LabCase(BaseModel):
    """One labelled case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    question: str = Field(min_length=1)
    scripted_calls: tuple[ScriptedCall, ...] = ()
    expects_tool_use: bool = False
    expected_outcome: str = Field(min_length=1)
    expected_tool: str | None = None
    expected_policy_decision: str | None = None
    expects_execution: bool = False
    rationale: str = Field(min_length=20)

    @model_validator(mode="after")
    def expectations_must_be_consistent(self) -> LabCase:
        if self.expects_tool_use and not self.expected_tool:
            raise ValueError("a case expecting tool use must name the tool")
        if self.expected_tool and not self.expects_tool_use:
            raise ValueError("a case naming a tool must expect tool use")
        if self.expected_outcome == "approval_required" and self.expects_execution:
            raise ValueError("an approval-required case must not expect execution")
        return self


class DatasetProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    authored: str
    relationship_to_phase_18: str
    what_fake_mode_proves: str
    what_fake_mode_cannot_prove: str
    limitations: tuple[str, ...] = Field(min_length=1)


class LabDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str = Field(min_length=1)
    dataset_version: int = Field(ge=1)
    provenance: DatasetProvenance
    cases: tuple[LabCase, ...] = Field(min_length=1)
    content_sha256: str = Field(min_length=64, max_length=64)

    @property
    def limitations(self) -> tuple[str, ...]:
        return self.provenance.limitations


def load_dataset(path: Path | None = None) -> LabDataset:
    """Load, validate, hash and check the composition of the set.

    The hash matters as much as the content: a report naming a version but not
    its bytes cannot say whether the set was edited between two runs.

    Raises:
        DatasetError: on a missing file, malformed JSON, schema failure, or a
            composition that has lost a required category.
    """
    target = path or DATASET_PATH
    try:
        raw_bytes = target.read_bytes()
    except OSError as exc:
        raise DatasetError(f"dataset could not be read: {target.name}") from exc
    try:
        raw = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise DatasetError(f"{target.name} is not valid JSON: {exc.msg}") from exc

    payload = {k: v for k, v in raw.items() if not k.startswith("$")}
    payload["content_sha256"] = hashlib.sha256(raw_bytes).hexdigest()
    try:
        dataset = LabDataset.model_validate(payload)
    except ValidationError as exc:
        locations = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        raise DatasetError(f"{target.name} failed validation at: {', '.join(locations)}") from exc

    for category, required in sorted(REQUIRED_CATEGORIES.items()):
        actual = sum(1 for case in dataset.cases if case.category == category)
        if actual < required:
            raise DatasetError(
                f"the set must retain at least {required} '{category}' case(s); found {actual}"
            )
    return dataset


__all__ = [
    "DATASET_PATH",
    "GATES_PATH",
    "DatasetError",
    "LabCase",
    "LabDataset",
    "ScriptedCall",
    "load_dataset",
]
