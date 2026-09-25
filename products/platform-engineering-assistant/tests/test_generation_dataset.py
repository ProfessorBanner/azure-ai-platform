"""The golden dataset: composition, validation, provenance and hygiene."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from platform_engineering_assistant.domain import AnswerStatus
from platform_engineering_assistant.errors import ConfigurationError
from platform_engineering_assistant.evaluation.generation_dataset import (
    DEFAULT_DATASET_PATH,
    REQUIRED_ADVERSARIAL_CASES,
    REQUIRED_ANSWERABLE_CASES,
    REQUIRED_AUTHORITY_CONFLICT_CASES,
    REQUIRED_CASE_COUNT,
    REQUIRED_INSUFFICIENT_EVIDENCE_CASES,
    REQUIRED_OUT_OF_SCOPE_CASES,
    Dataset,
    load_dataset,
)


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return load_dataset()


# --- composition ------------------------------------------------------------


def test_the_set_holds_exactly_sixteen_cases(dataset: Dataset) -> None:
    assert len(dataset.cases) == REQUIRED_CASE_COUNT == 16


def test_the_required_composition_is_present(dataset: Dataset) -> None:
    categories = [case.category for case in dataset.cases]
    assert categories.count("out-of-scope") == REQUIRED_OUT_OF_SCOPE_CASES
    assert categories.count("insufficient-evidence") == REQUIRED_INSUFFICIENT_EVIDENCE_CASES
    assert categories.count("adversarial") == REQUIRED_ADVERSARIAL_CASES
    assert categories.count("authority-conflict") == REQUIRED_AUTHORITY_CONFLICT_CASES

    plain = [
        case
        for case in dataset.cases
        if case.is_answerable and case.category not in {"adversarial", "authority-conflict"}
    ]
    assert len(plain) == REQUIRED_ANSWERABLE_CASES


def test_case_identifiers_are_unique(dataset: Dataset) -> None:
    identifiers = [case.case_id for case in dataset.cases]
    assert len(set(identifiers)) == len(identifiers)


# --- per-case contract ------------------------------------------------------


def test_every_answerable_case_names_its_evidence(dataset: Dataset) -> None:
    for case in dataset.answerable_cases:
        assert case.expected_doc_ids, case.case_id
        assert case.evidence is not None, case.case_id
        assert case.evidence.path and case.evidence.section and case.evidence.why


def test_every_refusable_case_allows_at_least_one_reason(dataset: Dataset) -> None:
    for case in dataset.refusable_cases:
        assert case.allowed_refusal_reasons, case.case_id
        assert not case.expected_doc_ids, case.case_id


def test_every_case_records_written_provenance(dataset: Dataset) -> None:
    """A case with no recorded reasoning cannot be reviewed, only trusted."""
    for case in dataset.cases:
        assert len(case.rationale) > 40, case.case_id


def test_every_expected_document_is_in_the_approved_corpus(
    dataset: Dataset, repo_root: Path
) -> None:
    manifest = json.loads(
        (
            repo_root / "products" / "platform-engineering-assistant" / "corpus" / "manifest.json"
        ).read_text()
    )
    approved = {document["doc_id"] for document in manifest["documents"]}
    for case in dataset.answerable_cases:
        assert set(case.expected_doc_ids) <= approved, case.case_id


# --- adversarial coverage ---------------------------------------------------


@pytest.mark.parametrize(
    "tag",
    [
        "prompt-injection",
        "prompt-disclosure",
        "credential-disclosure",
        "authority-conflict",
        "context-instructions",
        "unsupported-detail",
        "general-knowledge-trap",
    ],
)
def test_each_adversarial_behaviour_is_covered(dataset: Dataset, tag: str) -> None:
    """Coverage is asserted, not assumed.

    Each tag names one of the attacks Phase 17.2 committed to covering. A future
    edit that drops a case would otherwise leave the suite quietly narrower.
    """
    assert dataset.cases_tagged(tag), f"no case covers {tag}"


def test_the_prompt_disclosure_case_forbids_real_prompt_text(
    dataset: Dataset, repo_root: Path
) -> None:
    """The prohibited substrings must actually occur in the prompt being protected.

    Otherwise the check is decorative: it would pass whatever the model said.
    """
    prompt = (
        repo_root / "products" / "platform-engineering-assistant" / "prompts" / "answer_v1.md"
    ).read_text()
    case = dataset.cases_tagged("prompt-disclosure")[0]
    from_prompt = [
        needle for needle in case.prohibited_substrings if needle.lower() in prompt.lower()
    ]
    assert len(from_prompt) >= 2


def test_prohibited_substrings_are_never_empty_strings(dataset: Dataset) -> None:
    """An empty needle matches everything and would fail every case."""
    for case in dataset.cases:
        for needle in case.prohibited_substrings:
            assert needle.strip(), case.case_id


# --- provenance and honesty -------------------------------------------------


def test_the_set_records_that_it_is_not_an_independent_benchmark(dataset: Dataset) -> None:
    text = " ".join(dataset.limitations).lower()
    assert "not an independent benchmark" in text
    assert "small sample" in text or "sixteen cases is a small sample" in text


def test_the_measured_retrieval_ceiling_is_recorded(dataset: Dataset) -> None:
    """A ceiling that is not written down is rediscovered as a mystery failure."""
    assert "ceiling" in dataset.provenance.measured_retrieval_ceiling.lower()


def test_the_content_hash_is_of_the_file_bytes(dataset: Dataset, repo_root: Path) -> None:
    import hashlib

    path = (
        repo_root
        / "products"
        / "platform-engineering-assistant"
        / "evaluation"
        / "generation_v1.json"
    )
    assert dataset.content_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


# --- credential hygiene -----------------------------------------------------


# Assembled rather than written whole, for the same reason the corpus loader's
# JWT guard is: a scanner cannot tell a detection pattern apart from the thing it
# detects, so spelling the marker out here would make this test trip the
# repository's own `detect-private-key` hook.
CREDENTIAL_MARKERS = ("BEGIN " + "PRIVATE KEY", "AccountKey=", "client_secret", "password=")


@pytest.mark.parametrize("forbidden", CREDENTIAL_MARKERS)
def test_the_dataset_file_contains_no_credential_material(repo_root: Path, forbidden: str) -> None:
    path = (
        repo_root
        / "products"
        / "platform-engineering-assistant"
        / "evaluation"
        / "generation_v1.json"
    )
    assert forbidden.lower() not in path.read_text().lower()


# --- validation failures ----------------------------------------------------


def _write(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(payload))
    return path


def test_a_malformed_file_is_a_configuration_error(tmp_path: Path) -> None:
    path = tmp_path / "dataset.json"
    path.write_text("{not json")
    with pytest.raises(ConfigurationError, match="not valid JSON"):
        load_dataset(path)


def test_a_missing_file_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="could not be read"):
        load_dataset(tmp_path / "absent.json")


def test_the_wrong_case_count_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(DEFAULT_DATASET_PATH.read_text())
    payload["cases"] = payload["cases"][:5]
    with pytest.raises(ConfigurationError, match="exactly 16 cases"):
        load_dataset(_write(tmp_path, payload))


def test_an_answerable_case_without_expected_documents_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(DEFAULT_DATASET_PATH.read_text())
    payload["cases"][0]["expected_doc_ids"] = []
    with pytest.raises(ConfigurationError, match="failed schema validation"):
        load_dataset(_write(tmp_path, payload))


def test_a_refusable_case_without_allowed_reasons_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(DEFAULT_DATASET_PATH.read_text())
    for case in payload["cases"]:
        if case["expected_disposition"] == AnswerStatus.REFUSED.value:
            case["allowed_refusal_reasons"] = []
            break
    with pytest.raises(ConfigurationError, match="failed schema validation"):
        load_dataset(_write(tmp_path, payload))


def test_an_unknown_field_is_rejected(tmp_path: Path) -> None:
    """Closed schema: a typo must be loud, not silently ignored."""
    payload = json.loads(DEFAULT_DATASET_PATH.read_text())
    payload["cases"][0]["expcted_doc_ids"] = ["adr-0001"]
    with pytest.raises(ConfigurationError, match="failed schema validation"):
        load_dataset(_write(tmp_path, payload))
