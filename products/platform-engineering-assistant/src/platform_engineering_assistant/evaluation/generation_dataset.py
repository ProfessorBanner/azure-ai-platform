"""The golden generation / end-to-end evaluation set.

A dataset is loaded, validated and HASHED. The hash matters as much as the
content: a report that names a dataset version but not its bytes cannot tell you
whether the set was edited between two runs, and "the score went up" is a
different statement depending on the answer.

Every model here is closed and frozen, for the same reason the domain models
are: a misspelled key in a fixture must be a loud failure, not a silently
ignored field that leaves an expectation unchecked.

NOT AN INDEPENDENT BENCHMARK. The limitation is recorded in the file itself and
carried into every report; see `Dataset.limitations`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from platform_engineering_assistant.config import PRODUCT_ROOT
from platform_engineering_assistant.domain import AnswerStatus, RefusalReason
from platform_engineering_assistant.errors import ConfigurationError

DEFAULT_DATASET_PATH = PRODUCT_ROOT / "evaluation" / "generation_v1.json"

# The composition this set is required to hold. Asserted on load, not merely
# documented: the adversarial and refusal coverage is the reason the set exists,
# and a well-meaning edit that replaced an injection case with an eighth
# straightforward question would weaken the suite without failing anything.
REQUIRED_CASE_COUNT = 16
REQUIRED_ANSWERABLE_CASES = 8
REQUIRED_OUT_OF_SCOPE_CASES = 3
REQUIRED_INSUFFICIENT_EVIDENCE_CASES = 2
REQUIRED_ADVERSARIAL_CASES = 2
REQUIRED_AUTHORITY_CONFLICT_CASES = 1


class CaseEvidence(BaseModel):
    """Where the answer to an answerable case actually lives."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, description="Repository-relative document path.")
    section: str = Field(min_length=1, description="Heading, or headings, that carry the answer.")
    why: str = Field(min_length=1, description="Why this section answers the question.")


class GenerationCase(BaseModel):
    """One labelled end-to-end case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    expected_disposition: AnswerStatus
    expected_doc_ids: tuple[str, ...] = ()
    allowed_refusal_reasons: tuple[RefusalReason, ...] = ()
    prohibited_substrings: tuple[str, ...] = ()
    category: str = Field(min_length=1)
    tags: tuple[str, ...] = ()
    evidence: CaseEvidence | None = None
    rationale: str = Field(min_length=1, description="Written provenance for this case.")

    @model_validator(mode="after")
    def expectations_must_match_the_disposition(self) -> GenerationCase:
        """A case must be answerable or refusable, never vaguely both.

        The two directions are checked separately because they fail differently.
        An answerable case with no expected document cannot contribute to
        citation recall, so it would silently drop out of a metric rather than
        fail one. A refusable case with no allowed reason can never be scored
        correct at all.
        """
        if self.expected_disposition is AnswerStatus.ANSWERED:
            if not self.expected_doc_ids:
                raise ValueError("an answerable case must name at least one expected document")
            if self.evidence is None:
                raise ValueError("an answerable case must record its evidence path and section")
            if self.allowed_refusal_reasons:
                raise ValueError("an answerable case must not allow refusal reasons")
        else:
            if not self.allowed_refusal_reasons:
                raise ValueError("a refusable case must allow at least one refusal reason")
            if self.expected_doc_ids:
                raise ValueError("a refusable case must not expect cited documents")
        return self

    @property
    def is_answerable(self) -> bool:
        return self.expected_disposition is AnswerStatus.ANSWERED


class DatasetProvenance(BaseModel):
    """How the set was written, and what it does not prove."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    authored: str
    authoring_rule: str
    retrieval_was_not_tuned_to_this_set: str
    relationship_to_other_fixtures: str
    measured_retrieval_ceiling: str
    limitations: tuple[str, ...] = Field(min_length=1)


class Dataset(BaseModel):
    """A loaded, validated dataset and the hash of the bytes it came from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str = Field(min_length=1)
    dataset_version: int = Field(ge=1)
    corpus_version_at_authoring: int = Field(ge=1)
    provenance: DatasetProvenance
    cases: tuple[GenerationCase, ...] = Field(min_length=1)
    content_sha256: str = Field(min_length=64, max_length=64)

    @property
    def limitations(self) -> tuple[str, ...]:
        return self.provenance.limitations

    @property
    def answerable_cases(self) -> tuple[GenerationCase, ...]:
        return tuple(case for case in self.cases if case.is_answerable)

    @property
    def refusable_cases(self) -> tuple[GenerationCase, ...]:
        return tuple(case for case in self.cases if not case.is_answerable)

    def cases_tagged(self, tag: str) -> tuple[GenerationCase, ...]:
        return tuple(case for case in self.cases if tag in case.tags)


def _count_category(cases: tuple[GenerationCase, ...], category: str) -> int:
    return sum(1 for case in cases if case.category == category)


def _assert_composition(cases: tuple[GenerationCase, ...]) -> None:
    """Enforce the coverage the set promises.

    Phrased as required counts rather than minimums. The set is a fixed
    instrument: if it is to grow, the shape it grows into should be a reviewed
    decision recorded here, not an emergent property of whichever case someone
    added last.
    """
    if len(cases) != REQUIRED_CASE_COUNT:
        raise ConfigurationError(
            f"The generation dataset must contain exactly {REQUIRED_CASE_COUNT} cases."
        )

    identifiers = [case.case_id for case in cases]
    if len(set(identifiers)) != len(identifiers):
        raise ConfigurationError("Case identifiers must be unique.")

    # Counted by CATEGORY, not by tag. Several plain answerable cases carry an
    # `adversarial` tag because their retrieved evidence is instruction-shaped;
    # that is a property of the evidence, not a change of case class.
    special = {"adversarial", "authority-conflict"}
    answerable = len(
        [case for case in cases if case.is_answerable and case.category not in special]
    )
    expectations = (
        ("plain answerable", answerable, REQUIRED_ANSWERABLE_CASES),
        (
            "out-of-scope",
            _count_category(cases, "out-of-scope"),
            REQUIRED_OUT_OF_SCOPE_CASES,
        ),
        (
            "insufficient-evidence",
            _count_category(cases, "insufficient-evidence"),
            REQUIRED_INSUFFICIENT_EVIDENCE_CASES,
        ),
        (
            "adversarial",
            _count_category(cases, "adversarial"),
            REQUIRED_ADVERSARIAL_CASES,
        ),
        (
            "authority-conflict",
            _count_category(cases, "authority-conflict"),
            REQUIRED_AUTHORITY_CONFLICT_CASES,
        ),
    )
    for label, actual, required in expectations:
        if actual != required:
            raise ConfigurationError(
                f"The generation dataset must contain exactly {required} {label} case(s); "
                f"found {actual}."
            )


def load_dataset(path: Path | None = None) -> Dataset:
    """Load, validate and hash the versioned generation dataset.

    Raises:
        ConfigurationError: if the file is missing, malformed, fails schema
            validation, or does not hold the required composition. Messages name
            the rule that failed and never quote a question or an answer.
    """
    dataset_path = DEFAULT_DATASET_PATH if path is None else path

    try:
        raw_bytes = dataset_path.read_bytes()
    except OSError as exc:
        raise ConfigurationError(
            f"Generation dataset could not be read: {dataset_path.name}"
        ) from exc

    try:
        raw = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"{dataset_path.name} is not valid JSON: {exc.msg} (line {exc.lineno})"
        ) from exc

    if not isinstance(raw, dict):
        raise ConfigurationError(f"{dataset_path.name} must contain a JSON object.")

    payload = {key: value for key, value in raw.items() if not key.startswith("$")}
    payload["content_sha256"] = hashlib.sha256(raw_bytes).hexdigest()

    try:
        dataset = Dataset.model_validate(payload)
    except ValidationError as exc:
        # The field locations are reported; the offending values are not. A
        # question is content, and these messages are written to be logged.
        locations = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        raise ConfigurationError(
            f"{dataset_path.name} failed schema validation at: {', '.join(locations)}"
        ) from exc

    _assert_composition(dataset.cases)
    return dataset


__all__ = [
    "Dataset",
    "DatasetProvenance",
    "GenerationCase",
    "load_dataset",
]
