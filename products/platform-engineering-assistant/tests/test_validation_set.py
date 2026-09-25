"""The locked held-out validation set: separation, provenance and immutability.

Deliberately does NOT pin the measured recall. The set is a one-shot check;
asserting today's number would turn it into a target and make every future
change a negotiation with a test, which is exactly what a held-out set must not
become.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from platform_engineering_assistant.config import load_retrieval_config
from platform_engineering_assistant.corpus import load_corpus, load_manifest
from platform_engineering_assistant.corpus.chunking import chunk_corpus
from platform_engineering_assistant.evaluation.retrieval_baseline import (
    DEFAULT_BASELINE_PATH,
    load_cases,
)
from platform_engineering_assistant.evaluation.validation import (
    DATASET_ID,
    DEFAULT_VALIDATION_PATH,
    EXPECTED_CASE_COUNT,
    fixture_sha256,
    load_validation_set,
    render_validation,
    run_validation,
)
from platform_engineering_assistant.retrieval.index import build_index

SELECTED_TITLE_WEIGHT = 0.0
SELECTED_HEADING_WEIGHT = 0.25


def normalise(text: str) -> frozenset[str]:
    return frozenset(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


# --- the two fixtures are separate -----------------------------------------


def test_calibration_and_validation_are_different_files() -> None:
    assert DEFAULT_BASELINE_PATH != DEFAULT_VALIDATION_PATH
    assert DEFAULT_VALIDATION_PATH.name == "locked_validation_v1.json"


def test_validation_case_ids_do_not_overlap_calibration_ids() -> None:
    calibration = {case.case_id for case in load_cases()}
    validation = {entry.case.case_id for entry in load_validation_set()}
    assert not (calibration & validation)


def test_no_validation_question_duplicates_a_calibration_question() -> None:
    """Normalised token overlap, so a reworded duplicate is caught too."""
    calibration = [normalise(case.question) for case in load_cases()]
    for entry in load_validation_set():
        tokens = normalise(entry.case.question)
        for other in calibration:
            overlap = len(tokens & other) / max(len(tokens), 1)
            assert overlap <= 0.6, f"{entry.case.case_id} paraphrases a calibration question"


def test_validation_questions_are_unique_among_themselves() -> None:
    questions = [entry.case.question for entry in load_validation_set()]
    assert len(questions) == len(set(questions))


# --- shape and coverage -----------------------------------------------------


def test_validation_contains_exactly_eight_cases() -> None:
    assert len(load_validation_set()) == EXPECTED_CASE_COUNT == 8


def test_validation_covers_at_least_six_source_documents() -> None:
    documents: set[str] = set()
    for entry in load_validation_set():
        documents.update(entry.case.expected_doc_ids)
    assert len(documents) >= 6


def test_validation_covers_multiple_capability_categories() -> None:
    categories = {entry.category for entry in load_validation_set()}
    assert len(categories) >= 6


def test_every_expected_document_is_in_the_approved_manifest() -> None:
    approved = {document.doc_id for document in load_manifest().documents}
    for entry in load_validation_set():
        for doc_id in entry.case.expected_doc_ids:
            assert doc_id in approved


def test_every_case_carries_evidence_and_provenance() -> None:
    """A label without a source citation is an assertion, not evidence."""
    for entry in load_validation_set():
        assert entry.evidence_path.startswith("docs/")
        assert entry.evidence_section
        assert entry.evidence_lines
        assert len(entry.evidence_why) > 40


def test_every_cited_evidence_path_matches_an_expected_document(repo_root: Path) -> None:
    """The cited file must be the file the label points at, and must exist."""
    approved = {document.doc_id: document.path for document in load_manifest().documents}
    for entry in load_validation_set():
        cited = {approved[doc_id] for doc_id in entry.case.expected_doc_ids}
        assert entry.evidence_path in cited
        assert (repo_root / entry.evidence_path).is_file()


def test_dataset_declares_its_provenance_and_limits() -> None:
    payload = json.loads(DEFAULT_VALIDATION_PATH.read_text())
    assert payload["dataset_id"] == DATASET_ID
    provenance = payload["provenance"]
    assert "after calibration" in provenance["created"]
    assert "NOT statistically independent" in provenance["independence"]
    assert "not a quality claim" in provenance["intended_use"].lower()


def test_no_case_carries_synonyms_hints_or_scoring_overrides() -> None:
    """A held-out case must not smuggle in help the retriever would not have."""
    permitted = {"case_id", "question", "expected_doc_ids", "category", "evidence"}
    for case in json.loads(DEFAULT_VALIDATION_PATH.read_text())["cases"]:
        assert set(case) == permitted, f"{case['case_id']} carries unexpected fields"


# --- the shipped configuration remains the selected one --------------------


def test_shipped_weights_are_the_selected_ones() -> None:
    config = load_retrieval_config()
    assert config.title_weight == SELECTED_TITLE_WEIGHT
    assert config.heading_weight == SELECTED_HEADING_WEIGHT


# --- running validation is read-only ---------------------------------------


def test_running_validation_does_not_mutate_the_fixture() -> None:
    before = fixture_sha256()
    config = load_retrieval_config()
    chunks = chunk_corpus(load_corpus(load_manifest()), config.chunk_budget_chars)
    run_validation(build_index(chunks, config), load_validation_set(), config)
    assert fixture_sha256() == before


def test_report_names_the_fixture_it_measured() -> None:
    config = load_retrieval_config()
    chunks = chunk_corpus(load_corpus(load_manifest()), config.chunk_budget_chars)
    report = run_validation(build_index(chunks, config), load_validation_set(), config)
    assert report.fixture_sha256 == fixture_sha256()
    assert report.dataset_id == DATASET_ID


def test_rendered_report_contains_no_question_text() -> None:
    config = load_retrieval_config()
    chunks = chunk_corpus(load_corpus(load_manifest()), config.chunk_budget_chars)
    rendered = render_validation(
        run_validation(build_index(chunks, config), load_validation_set(), config)
    )
    for entry in load_validation_set():
        assert entry.case.question not in rendered


def test_rendered_report_states_it_is_not_a_generalisation_claim() -> None:
    config = load_retrieval_config()
    chunks = chunk_corpus(load_corpus(load_manifest()), config.chunk_budget_chars)
    rendered = render_validation(
        run_validation(build_index(chunks, config), load_validation_set(), config)
    )
    assert "not statistically independent" in rendered.lower()


def test_validation_is_reproducible_for_the_same_configuration() -> None:
    config = load_retrieval_config()
    chunks = chunk_corpus(load_corpus(load_manifest()), config.chunk_budget_chars)
    index = build_index(chunks, config)
    first = run_validation(index, load_validation_set(), config)
    second = run_validation(index, load_validation_set(), config)
    assert first.recall_at == second.recall_at
    assert first.missed == second.missed


@pytest.mark.parametrize("leak", ["VAL-0", "locked_validation", "expected_doc_ids"])
def test_retrieval_implementation_does_not_reference_the_validation_set(leak: str) -> None:
    """Scoring must not be able to recognise the set that judges it."""
    from platform_engineering_assistant.config import PRODUCT_ROOT

    source_root = PRODUCT_ROOT / "src" / "platform_engineering_assistant"
    for path in (source_root / "retrieval").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        assert leak not in path.read_text(), f"{path.name} references the validation set"
