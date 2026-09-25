"""The offline retrieval baseline: fixture integrity and measurement shape.

This suite asserts that the measurement is well-formed and reproducible. It
deliberately does NOT assert a recall figure: pinning today's number would turn
the baseline into a target and invite tuning the corpus or the fixture to keep a
test green, which is the opposite of measuring.
"""

from __future__ import annotations

import json

from platform_engineering_assistant.config import RetrievalConfig, load_retrieval_config
from platform_engineering_assistant.corpus import load_corpus, load_manifest
from platform_engineering_assistant.corpus.chunking import Chunk, chunk_corpus
from platform_engineering_assistant.corpus.manifest import DEFAULT_MANIFEST_PATH
from platform_engineering_assistant.evaluation.retrieval_baseline import (
    RECALL_DEPTHS,
    load_cases,
    measure_baseline,
    render_report,
)
from platform_engineering_assistant.retrieval.index import BM25Index, build_index

REQUIRED_TOPICS = {
    "four environments",
    "terraform state",
    "foundry authentication",
    "foundry networking",
    "databricks promotion",
    "unity catalog isolation",
    "ml monitoring",
    "incident recovery",
    "product contract",
    "multi-document",
}


def build() -> tuple[BM25Index, list[Chunk], RetrievalConfig]:
    config = load_retrieval_config()
    chunks = chunk_corpus(load_corpus(load_manifest()), config.chunk_budget_chars)
    return build_index(chunks, config), chunks, config


# --- fixture integrity ------------------------------------------------------


def test_fixture_has_enough_answerable_and_out_of_scope_cases() -> None:
    cases = load_cases()
    answerable = [case for case in cases if not case.must_refuse]
    out_of_scope = [case for case in cases if case.must_refuse]
    assert len(answerable) >= 10
    assert len(out_of_scope) >= 3


def test_fixture_covers_every_required_topic() -> None:
    topics = {case.topic for case in load_cases()}
    missing = REQUIRED_TOPICS - topics
    assert not missing, f"fixture does not cover: {sorted(missing)}"


def test_every_expected_doc_id_exists_in_the_manifest() -> None:
    approved = {d["doc_id"] for d in json.loads(DEFAULT_MANIFEST_PATH.read_text())["documents"]}
    for case in load_cases():
        for doc_id in case.expected_doc_ids:
            assert doc_id in approved, f"{case.case_id} expects unknown doc_id {doc_id}"


def test_out_of_scope_cases_expect_no_documents() -> None:
    for case in load_cases():
        if case.must_refuse:
            assert case.expected_doc_ids == ()


def test_answerable_cases_expect_at_least_one_document() -> None:
    for case in load_cases():
        if not case.must_refuse:
            assert case.expected_doc_ids


def test_at_least_one_case_expects_multiple_documents() -> None:
    assert any(len(case.expected_doc_ids) > 1 for case in load_cases())


def test_case_ids_are_unique() -> None:
    identifiers = [case.case_id for case in load_cases()]
    assert len(identifiers) == len(set(identifiers))


# --- measurement ------------------------------------------------------------


def test_baseline_measures_every_case() -> None:
    index, chunks, config = build()
    report = measure_baseline(index, chunks, load_cases(), config)
    assert len(report.outcomes) == len(load_cases())
    assert report.answerable_cases + report.out_of_scope_cases == len(load_cases())


def test_recall_is_reported_at_every_depth_and_is_monotonic() -> None:
    index, chunks, config = build()
    report = measure_baseline(index, chunks, load_cases(), config)
    values = [report.recall_at[depth] for depth in RECALL_DEPTHS]
    assert set(report.recall_at) == set(RECALL_DEPTHS)
    assert values == sorted(values), "recall@k must not decrease as k grows"
    assert all(0.0 <= value <= 1.0 for value in values)


def test_corpus_statistics_are_reported() -> None:
    index, chunks, config = build()
    report = measure_baseline(index, chunks, load_cases(), config)
    assert report.chunk_count == len(chunks)
    assert report.duplicate_chunk_ids == 0
    assert 0 < report.min_chunk_chars <= report.median_chunk_chars <= report.max_chunk_chars
    assert report.mean_chunk_chars > 0


def test_baseline_is_reproducible() -> None:
    index, chunks, config = build()
    cases = load_cases()
    first = measure_baseline(index, chunks, cases, config)
    second = measure_baseline(index, chunks, cases, config)
    assert first.recall_at == second.recall_at
    assert first.answerable_top_scores == second.answerable_top_scores
    assert first.missed_cases == second.missed_cases


def test_baseline_runs_entirely_offline() -> None:
    """No provider, no credential, no network: it is pure computation."""
    index, chunks, config = build()
    report = measure_baseline(index, chunks, load_cases(), config)
    assert report.chunk_count > 0


# --- report redaction -------------------------------------------------------


def test_report_contains_no_chunk_text() -> None:
    index, chunks, config = build()
    rendered = render_report(measure_baseline(index, chunks, load_cases(), config))
    for chunk in chunks[:40]:
        excerpt = chunk.text[:60].strip()
        if len(excerpt) > 30:
            assert excerpt not in rendered


def test_report_contains_no_question_text() -> None:
    index, chunks, config = build()
    rendered = render_report(measure_baseline(index, chunks, load_cases(), config))
    for case in load_cases():
        assert case.question not in rendered


def test_outcomes_carry_ids_and_numbers_only() -> None:
    index, chunks, config = build()
    outcome = measure_baseline(index, chunks, load_cases(), config).outcomes[0]
    fields = set(outcome.__slots__)
    assert fields == {
        "case_id",
        "must_refuse",
        "returned",
        "top_score",
        "retrieved_doc_ids",
        "first_hit_rank",
        "empty",
    }
